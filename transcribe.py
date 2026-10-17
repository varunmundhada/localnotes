"""Speech-to-text with faster-whisper on CPU.

Live pass: short chunks transcribed as they arrive, using the previous sentence
and the user's vocabulary as context so chunks read continuously.
Refine pass: after the meeting, the full per-speaker recordings are transcribed
again with a larger model and replace the live draft.
"""
import difflib
import queue
import threading
import time
import wave

import numpy as np
from faster_whisper import BatchedInferencePipeline, WhisperModel

# Whisper tends to invent these on near-silent audio.
HALLUCINATIONS = {
    "thank you.", "thanks for watching!", "thank you for watching.", "you",
    "bye.", "thank you so much.", ".", "okay.",
}
ECHO_WINDOW_S = 12.0     # how far apart a Me/Them pair can be and still count as echo
ECHO_SIMILARITY = 0.75
TARGET_PEAK = 0.9
MAX_GAIN = 20.0          # don't boost quiet mics so far that background hiss dominates
PROMPT_CHARS = 220       # previous text carried into the next chunk
REFINE_THREADS = 8       # batched refine on an i5-1340P: 8 threads/batch 8 measured fastest
REFINE_BATCH = 8


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
    return WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=threads)


class Transcriber:
    """Live pass. Queue chunks; results arrive via on_segment(meeting_id, speaker, t0, t1, text, channel).

    identify(channel, audio) may return a person's name for the chunk (known voice), else None.
    """

    def __init__(self, model_name: str, on_segment, on_status=None, identify=None):
        self.model_name = model_name
        self.vocabulary = ""
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
                self._load()
                key = (mid, speaker)
                segments, _ = self._model.transcribe(
                    normalize(audio),
                    language="en" if self.model_name.endswith(".en") else None,
                    vad_filter=True,
                    beam_size=5,
                    condition_on_previous_text=False,
                    initial_prompt=build_prompt(self.vocabulary, self._previous.get(key, "")),
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
           turns: list[dict] | None = None) -> list[dict]:
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
            language="en" if model_name.endswith(".en") else None,
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
