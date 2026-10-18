"""Speech-to-text: Whisper large-v3 on Groq (cloud mode), or faster-whisper on the CPU (local mode).

Live pass: short chunks transcribed as they arrive, using the previous sentence
and the user's vocabulary as context so chunks read continuously. In cloud mode a
chunk that can't be sent (offline, rate limit, no key) is reported as a gap and
filled in from the saved recording later (cloud_fill).
Refine pass (local mode): after the meeting, the full per-speaker recordings are
transcribed again with a larger model and replace the live draft.
"""
import difflib
import queue
import threading
import time
import wave
from types import SimpleNamespace

import numpy as np
from faster_whisper import BatchedInferencePipeline, WhisperModel

from cloud import AuthError, Offline, RateLimited, with_retries
from languages import LOCAL_MODEL_DIRS

# Whisper tends to invent these on near-silent audio.
HALLUCINATIONS = {
    "thank you.", "thanks for watching!", "thank you for watching.", "you",
    "bye.", "thank you so much.", ".", "okay.",
    "nan", "nan.",   # Hinglish fine-tunes emit this on near-silent, noisy audio
}
ECHO_WINDOW_S = 12.0     # how far apart a Me/Them pair can be and still count as echo
ECHO_SIMILARITY = 0.75
TARGET_PEAK = 0.9
MAX_GAIN = 20.0          # don't boost quiet mics so far that background hiss dominates
PROMPT_CHARS = 220       # previous text carried into the next chunk
REFINE_THREADS = 8       # batched refine on an i5-1340P: 8 threads/batch 8 measured fastest
REFINE_BATCH = 8
OFFLINE_RETRY_S = 20.0   # after a failed cloud call, queue chunks straight away for this long instead of retrying each


def normalize(audio: np.ndarray) -> np.ndarray:
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    if peak <= 1e-4:
        return audio
    return (audio * min(MAX_GAIN, TARGET_PEAK / peak)).astype(np.float32)


def build_prompt(vocabulary: str, previous: str = "") -> str | None:
    """Whisper's initial prompt biases spelling toward these words and keeps context."""
    vocab = ", ".join(w.strip() for w in vocabulary.replace("\n", ",").split(",") if w.strip())
    parts = []
    if vocab:
        parts.append(f"Glossary: {vocab}.")
    if previous:
        parts.append(previous[-PROMPT_CHARS:])
    return " ".join(parts) or None


def keep_segment(s) -> bool:
    text = s.text.strip()
    if not text or text.lower() in HALLUCINATIONS:
        return False
    return not (s.no_speech_prob > 0.6 and s.avg_logprob < -1.0)


def is_echo(t: float, text: str, them: list[tuple[float, str]]) -> bool:
    """Mic picking up the speakers: 'Me' text that repeats nearby 'Them' text."""
    low = text.lower()
    return any(
        abs(ts - t) <= ECHO_WINDOW_S and difflib.SequenceMatcher(None, low, other).ratio() >= ECHO_SIMILARITY
        for ts, other in them
    )


def load_model(name: str, threads: int = 0) -> WhisperModel:
    """name is a faster-whisper model id, or one of the locally converted models in languages.LOCAL_MODEL_DIRS."""
    source = str(LOCAL_MODEL_DIRS[name]) if name in LOCAL_MODEL_DIRS else name
    return WhisperModel(source, device="cpu", compute_type="int8", cpu_threads=threads)


def _language_for(model_name: str, language: str | None) -> str | None:
    return "en" if model_name.endswith(".en") else language


class Transcriber:
    """Live pass. Queue chunks; results arrive via on_segment(meeting_id, speaker, t0, t1, text, channel).

    identify(channel, audio) may return a person's name for the chunk (known voice), else None.
    """

    def __init__(self, model_name: str, on_segment, on_status=None, identify=None):
        self.model_name = model_name
        self.language: str | None = "en"     # Whisper language hint; None = auto-detect
        self.vocabulary = ""
        self.cloud = None                     # cloud.Groq when processing in the cloud, else local Whisper
        self.seed = ""                        # prompt for a speaker's first chunk (cloud Hinglish)
        self.on_gap = lambda mid, speaker, t0, t1: None   # a chunk the cloud couldn't take: fill in later
        self.on_cloud = lambda ok, reason: None           # cloud reachable again / not (with the reason)
        self._cloud_ok = True
        self._offline_until = 0.0
        self.on_segment = on_segment
        self.on_status = on_status or (lambda s: None)
        self.identify = identify or (lambda channel, audio: None)
        self._q: queue.Queue = queue.Queue()
        self._model = None
        self._recent_them: list[tuple[float, str]] = []
        self._previous: dict[tuple[int, str], str] = {}
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, meeting_id: int, speaker: str, t_start: float, audio):
        self._q.put((meeting_id, speaker, t_start, audio))

    def pending(self) -> int:
        return self._q.qsize()

    def wait_idle(self, timeout: float = 600):
        end = time.time() + timeout
        while (self._q.unfinished_tasks > 0) and time.time() < end:
            time.sleep(0.2)

    def unload(self):
        self._model = None

    def _load(self):
        if self._model is None:
            self.on_status(f"Loading speech model '{self.model_name}' (first run downloads it)…")
            self._model = load_model(self.model_name)
            self.on_status("")

    def _run(self):
        while True:
            mid, speaker, t0, audio = self._q.get()
            try:
                key = (mid, speaker)
                prompt = build_prompt(self.vocabulary, self._previous.get(key, "") or self.seed)
                if self.cloud:
                    segments = self._cloud_transcribe(audio, prompt)
                    if segments is None:
                        self.on_gap(mid, speaker, t0, t0 + len(audio) / 16000)
                        continue
                else:
                    self._load()
                    segments, _ = self._model.transcribe(
                        normalize(audio),
                        language=_language_for(self.model_name, self.language),
                        vad_filter=True,
                        beam_size=5,
                        condition_on_previous_text=False,
                        initial_prompt=prompt,
                    )
                name = None
                for s in segments:
                    if not keep_segment(s):
                        continue
                    text = s.text.strip()
                    a, b = t0 + s.start, t0 + s.end
                    if speaker == "Them":
                        self._recent_them.append((a, text.lower()))
                        self._recent_them = [x for x in self._recent_them if a - x[0] < 60]
                    elif is_echo(a, text, self._recent_them):
                        continue
                    if name is None:   # fingerprint the chunk once, only if it has kept speech
                        try:
                            name = self.identify(speaker, audio) or ""
                        except Exception:
                            name = ""
                    self._previous[key] = (self._previous.get(key, "") + " " + text)[-PROMPT_CHARS:]
                    self.on_segment(mid, name or speaker, a, b, text, speaker)
            except Exception as e:  # keep the worker alive; report and move on
                self.on_status(f"Transcription error: {e}")
            finally:
                self._q.task_done()

    def cloud_reachable(self):
        """The cloud answered elsewhere (catch-up, connection test): stop queueing live chunks straight away."""
        self._offline_until = 0.0

    def _cloud_transcribe(self, audio, prompt):
        """-> segments, or None if the chunk has to wait for the connection / key."""
        if time.time() < self._offline_until:
            return None
        try:
            segs = with_retries(lambda: self.cloud.transcribe(normalize(audio), self.language, prompt),
                                max_wait=15)   # live: don't hold up the queue; a gap is filled after the call
        except (Offline, RateLimited, AuthError) as e:
            self._offline_until = time.time() + OFFLINE_RETRY_S
            if self._cloud_ok:
                self._cloud_ok = False
                self.on_cloud(False, str(e))
            return None
        if not self._cloud_ok:
            self._cloud_ok = True
            self.on_cloud(True, "")
        return [SimpleNamespace(**s) for s in segs]


FILL_MAX_S = 60.0        # merge adjacent gaps into requests of up to a minute (Groq bills at least 10 s each)


def merge_gaps(gaps: list[dict], max_s: float = FILL_MAX_S) -> list[dict]:
    """[{channel, t_start, t_end}] -> fewer, larger ranges per channel (adjacent or overlapping, up to max_s)."""
    out: list[dict] = []
    for g in sorted(gaps, key=lambda g: (g["channel"], g["t_start"])):
        last = out[-1] if out else None
        if last and last["channel"] == g["channel"] and g["t_start"] - last["t_end"] < 1.0 \
                and g["t_end"] - last["t_start"] <= max_s:
            last["t_end"] = max(last["t_end"], g["t_end"])
        else:
            out.append(dict(g))
    return out


def split_ranges(ranges: list[dict], piece_s: float) -> list[dict]:
    """Cut ranges longer than piece_s into consecutive pieces."""
    out = []
    for r in ranges:
        a = r["t_start"]
        while r["t_end"] - a > 0.05:
            b = min(r["t_end"], a + piece_s)
            out.append({**r, "t_start": a, "t_end": b})
            a = b
    return out


def cloud_fill(client, tracks: dict, gaps: list[dict], language: str | None, vocabulary: str = "",
               on_progress=None, seed: str = "", piece_s: float = FILL_MAX_S) -> list[dict]:
    """Transcribe the stretches the live pass missed, from the saved per-channel WAVs.

    Raises cloud.Offline / RateLimited / AuthError so the caller can keep the gaps queued for later.
    Returns [{speaker, channel, t_start, t_end, text}].
    """
    on_progress = on_progress or (lambda s: None)
    audio = {ch: _read_wav(p) for ch, p in tracks.items()}
    ranges = split_ranges([r for r in merge_gaps(gaps) if r["channel"] in audio], piece_s)
    them: list[tuple[float, str]] = []
    results = []
    for i, r in enumerate(sorted(ranges, key=lambda r: r["channel"] != "Them")):   # Them first, for echo removal
        on_progress(f"Transcribing in the cloud: {int(100 * i / len(ranges))}%")
        piece = audio[r["channel"]][int(r["t_start"] * 16000): int(r["t_end"] * 16000)]
        if len(piece) < 8000 or not np.any(np.abs(piece) > 0.01):
            continue
        segs = with_retries(lambda: client.transcribe(normalize(piece), language, build_prompt(vocabulary, seed)))
        for s in map(lambda d: SimpleNamespace(**d), segs):
            if not keep_segment(s):
                continue
            text, a = s.text.strip(), r["t_start"] + s.start
            if r["channel"] == "Them":
                them.append((a, text.lower()))
            elif is_echo(a, text, them):
                continue
            results.append({"speaker": r["channel"], "channel": r["channel"], "t_start": a,
                            "t_end": r["t_start"] + s.end, "text": text})
    return results


def _read_wav(path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return pcm.astype(np.float32) / 32768.0


MAX_CLIP_S = 28.0        # Whisper's window is 30 s
MERGE_GAP_S = 0.8        # consecutive turns of the same voice closer than this become one clip


def turn_clips(turns: list[dict], channel: str, n_samples: int) -> list[dict] | None:
    """Speaker turns of one channel -> clip_timestamps (in samples) for the batched pipeline.

    Without clips the pipeline packs up to 30 s of speech into each segment, so one
    line can hold several people and speaker labels get lost. Clipping at turn
    boundaries keeps every line to a single voice.
    """
    spans = sorted((t["t_start"], t["t_end"], t["cluster"]) for t in turns if t["channel"] == channel)
    merged: list[list] = []
    for a, b, c in spans:
        if merged and merged[-1][2] == c and a - merged[-1][1] < MERGE_GAP_S and b - merged[-1][0] <= MAX_CLIP_S:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b, c])
    clips = []
    for a, b, _ in merged:
        while b - a > 0.3:
            end = min(b, a + MAX_CLIP_S)
            s, e = int(a * 16000), min(int(end * 16000), n_samples)
            if e > s:
                clips.append({"start": s, "end": e})
            a = end
    return clips or None


def refine(model_name: str, tracks: dict, vocabulary: str = "", on_progress=None,
           turns: list[dict] | None = None, language: str | None = "en") -> list[dict]:
    """Re-transcribe full per-speaker recordings with a stronger model.

    tracks maps speaker -> WAV path; turns (optional) are the diarized voice turns,
    used as clip boundaries so each line belongs to one voice. Returns segments
    sorted by time: [{speaker, channel, t_start, t_end, text}]. 'Them' is processed
    first so echo of it can be removed from 'Me'.
    """
    on_progress = on_progress or (lambda s: None)
    on_progress(f"Loading high-accuracy model '{model_name}' (first use downloads it)…")
    # Batched inference transcribes several VAD-split speech regions at once: ~2-7x faster on CPU.
    model = BatchedInferencePipeline(model=load_model(model_name, REFINE_THREADS))
    results: list[dict] = []
    them: list[tuple[float, str]] = []
    for speaker in sorted(tracks, key=lambda s: s != "Them"):
        audio = _read_wav(tracks[speaker])
        total = len(audio) / 16000
        if total < 1:
            continue
        segments, _ = model.transcribe(
            normalize(audio),
            language=_language_for(model_name, language),
            batch_size=REFINE_BATCH,
            beam_size=5,
            initial_prompt=build_prompt(vocabulary),
            clip_timestamps=turn_clips(turns, speaker, len(audio)) if turns else None,
        )
        for s in segments:
            on_progress(f"Improving transcript ({speaker}): {int(100 * s.end / total)}%")
            if not keep_segment(s):
                continue
            text = s.text.strip()
            if speaker == "Them":
                them.append((s.start, text.lower()))
            elif is_echo(s.start, text, them):
                continue
            results.append({"speaker": speaker, "channel": speaker, "t_start": s.start, "t_end": s.end, "text": text})
    results.sort(key=lambda r: r["t_start"])
    return results
