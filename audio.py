"""Capture microphone ("Me") and system output via WASAPI loopback ("Them").

Each source is resampled to 16 kHz mono and cut into utterance-sized chunks at
pauses, which are handed to a callback for live transcription. Optionally each
source is also written to a 16 kHz WAV on a wall-clock timeline (silence padded
in where the device delivered nothing) for a higher-quality pass afterwards.
"""
import threading
import time
import wave
from pathlib import Path

import numpy as np
import pyaudiowpatch as pyaudio
import soxr

TARGET_RATE = 16000
MIN_CHUNK_S = 5.0       # don't cut before this much audio
MAX_CHUNK_S = 20.0      # always cut at this length
TAIL_S = 0.5            # trailing window checked for a pause
PAUSE_RMS = 0.006       # below this the tail counts as a pause
SPEECH_RMS = 0.012      # a chunk with no 30 ms window above this is dropped as silence
IDLE_FLUSH_S = 1.5      # loopback delivers nothing while output is silent; flush after this gap
DRIFT_PAD_S = 0.25      # pad the WAV with silence when it falls this far behind the clock


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x * x))) if len(x) else 0.0


def _has_speech(x: np.ndarray) -> bool:
    win = int(0.03 * TARGET_RATE)
    n = len(x) // win
    if n == 0:
        return False
    frames = x[: n * win].reshape(n, win)
    return float(np.sqrt((frames ** 2).mean(axis=1)).max()) >= SPEECH_RMS


class _Segmenter:
    """Accumulates one source's audio and emits chunks at natural pauses."""

    def __init__(self, speaker: str, on_chunk):
        self.speaker = speaker
        self.on_chunk = on_chunk
        self.buf: list[np.ndarray] = []
        self.n = 0
        self.t0 = None
        self.last_audio = 0.0
        self.lock = threading.Lock()
        self.level = 0.0

    def push(self, audio: np.ndarray, t_block_start: float):
        with self.lock:
            if self.t0 is None:
                self.t0 = t_block_start
            self.buf.append(audio)
            self.n += len(audio)
            self.last_audio = time.monotonic()
            self.level = _rms(audio)
            dur = self.n / TARGET_RATE
            if dur >= MAX_CHUNK_S:
                self._flush()
            elif dur >= MIN_CHUNK_S:
                tail = np.concatenate(self.buf)[-int(TAIL_S * TARGET_RATE):]
                if _rms(tail) < PAUSE_RMS:
                    self._flush()

    def tick(self):
        with self.lock:
            if self.n and time.monotonic() - self.last_audio > IDLE_FLUSH_S:
                self._flush()

    def flush(self):
        with self.lock:
            self._flush()

    def _flush(self):
        if not self.n:
            return
        audio = np.concatenate(self.buf)
        t0 = self.t0
        self.buf, self.n, self.t0 = [], 0, None
        if _has_speech(audio):
            self.on_chunk(self.speaker, t0, audio)


class _WavTrack:
    """16 kHz int16 WAV whose sample position tracks the meeting clock."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._w = wave.open(str(path), "wb")
        self._w.setnchannels(1)
        self._w.setsampwidth(2)
        self._w.setframerate(TARGET_RATE)
        self._written = 0
        self._lock = threading.Lock()

    def write(self, audio: np.ndarray, t_start: float):
        with self._lock:
            if self._w is None:
                return
            gap = int(t_start * TARGET_RATE) - self._written
            if gap > DRIFT_PAD_S * TARGET_RATE:
                self._w.writeframes(np.zeros(gap, dtype=np.int16).tobytes())
                self._written += gap
            pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
            self._w.writeframes(pcm.tobytes())
            self._written += len(pcm)

    def close(self):
        with self._lock:
            if self._w is not None:
                self._w.close()
                self._w = None


class Recorder:
    """Records both sources until stop(). on_chunk(speaker, t_start_s, audio16k).

    If audio_dir is given, each source is also saved as <audio_dir>/<speaker>.wav.
    """

    def __init__(self, on_chunk, capture_system: bool = True, capture_mic: bool = True,
                 audio_dir: Path | None = None):
        self.on_chunk = on_chunk
        self.capture_system = capture_system
        self.capture_mic = capture_mic
        self.audio_dir = audio_dir
        self._pa = None
        self._streams = []
        self._segs: dict[str, _Segmenter] = {}
        self._tracks: dict[str, _WavTrack] = {}
        self._t_start = 0.0
        self._running = False
        self._ticker = None
        self.paused = False
        self.devices: dict[str, str] = {}

    def pause(self):
        """Stop capturing (off the record) until resume(); what was said so far is flushed first."""
        self.paused = True
        for s in self._segs.values():
            s.flush()
            s.level = 0.0

    def resume(self):
        self.paused = False

    def levels(self) -> dict:
        return {k: round(s.level, 4) for k, s in self._segs.items()}

    def tracks(self) -> dict[str, Path]:
        return {k: t.path for k, t in self._tracks.items()}

    def start(self):
        self._pa = pyaudio.PyAudio()
        self._t_start = time.monotonic()
        self._running = True
        wasapi = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        if self.capture_mic:
            mic = self._pa.get_device_info_by_index(wasapi["defaultInputDevice"])
            self._open("Me", mic)
        if self.capture_system:
            self._open("Them", self._pa.get_default_wasapi_loopback())
        self._ticker = threading.Thread(target=self._tick_loop, daemon=True)
        self._ticker.start()

    def _open(self, speaker: str, dev: dict):
        seg = _Segmenter(speaker, self.on_chunk)
        self._segs[speaker] = seg
        track = None
        if self.audio_dir:
            track = self._tracks[speaker] = _WavTrack(self.audio_dir / f"{speaker}.wav")
        channels = max(1, int(dev["maxInputChannels"]))
        rate = int(dev["defaultSampleRate"])
        # Streaming high-quality resampler keeps filter state across blocks (no edge clicks).
        resampler = soxr.ResampleStream(rate, TARGET_RATE, 1, dtype="float32", quality="HQ")
        self.devices[speaker] = dev["name"]
        t_start = self._t_start

        def cb(in_data, frame_count, time_info, status):
            if self.paused:   # dropped entirely; the WAV gets silence padded in on resume
                return (None, pyaudio.paContinue)
            block = np.frombuffer(in_data, dtype=np.float32)
            if channels > 1:
                block = block.reshape(-1, channels).mean(axis=1)
            audio = resampler.resample_chunk(np.ascontiguousarray(block, dtype=np.float32))
            t = max(0.0, time.monotonic() - t_start - frame_count / rate)
            if len(audio):
                seg.push(audio, t)
                if track:
                    track.write(audio, t)
            return (None, pyaudio.paContinue)

        stream = self._pa.open(
            format=pyaudio.paFloat32,
            channels=channels,
            rate=rate,
            input=True,
            input_device_index=dev["index"],
            frames_per_buffer=int(rate * 0.1),
            stream_callback=cb,
        )
        stream.start_stream()
        self._streams.append(stream)

    def _tick_loop(self):
        while self._running:
            time.sleep(0.25)
            for s in list(self._segs.values()):
                s.tick()

    def stop(self):
        self._running = False
        for st in self._streams:
            try:
                st.stop_stream()
                st.close()
            except Exception:
                pass
        self._streams.clear()
        for s in self._segs.values():
            s.flush()
        for t in self._tracks.values():
            t.close()
        if self._pa:
            self._pa.terminate()
            self._pa = None
