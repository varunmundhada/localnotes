"""Who said what: split each recording into voices and recognise known people.

- diarize(): sherpa-onnx (pyannote segmentation + TitaNet embeddings) splits a
  16 kHz track into turns labelled by voice cluster.
- Each cluster gets a voice fingerprint (L2-normalised embedding, averaged over
  its turns). Fingerprints are compared with saved voice profiles by cosine
  similarity; a profile is just a running average of a person's fingerprints.
"""
import os
import threading
from pathlib import Path

import numpy as np
import sherpa_onnx

MODELS_DIR = Path(os.environ.get("APPDATA", Path.home())) / "LocalNotes" / "models"
SEG_MODEL = MODELS_DIR / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx"
EMB_MODEL = MODELS_DIR / "nemo_en_titanet_small.onnx"
RATE = 16000

CLUSTER_THRESHOLD = 0.5   # sherpa clustering distance; lower splits voices more eagerly
MATCH_THRESHOLD = 0.60    # cosine similarity to accept a saved voice profile
MATCH_MARGIN = 0.05       # best profile must beat the runner-up by this much
MIN_TURN_S = 1.0          # turns shorter than this are too short to fingerprint well
MAX_EMBED_S = 12.0        # cap per-turn audio fed to the embedder


def available() -> bool:
    return SEG_MODEL.exists() and EMB_MODEL.exists()


def _norm(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n else v


class SpeakerEngine:
    """Lazily loads the models once; safe to call from worker threads."""

    def __init__(self):
        self._lock = threading.Lock()
        self._diarizer = None
        self._extractor = None

    def _load(self):
        if self._extractor is None:
            self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
                sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(EMB_MODEL), num_threads=4))
        if self._diarizer is None:
            cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
                segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                    pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(SEG_MODEL)),
                    num_threads=4),
                embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(EMB_MODEL), num_threads=4),
                clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=CLUSTER_THRESHOLD),
                min_duration_on=0.3,
                min_duration_off=0.5,
            )
            self._diarizer = sherpa_onnx.OfflineSpeakerDiarization(cfg)

    def embed(self, audio: np.ndarray) -> np.ndarray | None:
        """Voice fingerprint of one stretch of speech (None if too short)."""
        if len(audio) < MIN_TURN_S * RATE:
            return None
        with self._lock:
            self._load()
            s = self._extractor.create_stream()
            s.accept_waveform(RATE, audio[: int(MAX_EMBED_S * RATE)].astype(np.float32))
            s.input_finished()
            return _norm(np.array(self._extractor.compute(s), dtype=np.float32))

    def diarize(self, audio: np.ndarray) -> tuple[list[dict], dict[int, np.ndarray]]:
        """Returns (turns, fingerprints): turns = [{t_start, t_end, cluster}], one fingerprint per cluster."""
        if len(audio) < RATE:
            return [], {}
        with self._lock:
            self._load()
            result = self._diarizer.process(audio.astype(np.float32)).sort_by_start_time()
        turns = [{"t_start": r.start, "t_end": r.end, "cluster": int(r.speaker)} for r in result]
        prints: dict[int, list[tuple[float, np.ndarray]]] = {}
        for t in sorted(turns, key=lambda t: t["t_start"] - t["t_end"])[:400]:   # longest turns first
            if len(prints.get(t["cluster"], [])) >= 8:
                continue
            e = self.embed(audio[int(t["t_start"] * RATE): int(t["t_end"] * RATE)])
            if e is not None:
                prints.setdefault(t["cluster"], []).append((t["t_end"] - t["t_start"], e))
        fingerprints = {c: _norm(sum(w * e for w, e in v)) for c, v in prints.items()}
        return turns, fingerprints


def best_match(fp: np.ndarray, profiles: dict[str, np.ndarray]) -> tuple[str | None, float]:
    """Name of the saved voice closest to fp, if it is close enough and unambiguous."""
    if fp is None or not profiles:
        return None, 0.0
    scored = sorted(((float(np.dot(fp, p)), name) for name, p in profiles.items()), reverse=True)
    best, name = scored[0]
    runner = scored[1][0] if len(scored) > 1 else -1.0
    if best >= MATCH_THRESHOLD and best - runner >= MATCH_MARGIN:
        return name, best
    return None, best


def merge_profile(old: np.ndarray | None, old_n: int, new: np.ndarray) -> tuple[np.ndarray, int]:
    """Running average of fingerprints; caps the weight so a profile keeps adapting."""
    if old is None:
        return _norm(new), 1
    w = min(old_n, 20)
    return _norm(old * w + new), old_n + 1


def label_segments(segments: list[dict], turns: list[dict], channel: str) -> None:
    """Attach the overlapping voice cluster to each transcript segment of one channel (in place)."""
    chan_turns = [t for t in turns if t["channel"] == channel]
    for s in segments:
        if s.get("channel", s["speaker"]) != channel:
            continue
        overlap: dict[int, float] = {}
        for t in chan_turns:
            o = min(s["t_end"], t["t_end"]) - max(s["t_start"], t["t_start"])
            if o > 0:
                overlap[t["cluster"]] = overlap.get(t["cluster"], 0.0) + o
        if overlap:
            s["cluster"] = f"{channel}:{max(overlap, key=overlap.get)}"
        elif chan_turns:   # no overlap: nearest turn in time
            mid = (s["t_start"] + s["t_end"]) / 2
            near = min(chan_turns, key=lambda t: min(abs(t["t_start"] - mid), abs(t["t_end"] - mid)))
            s["cluster"] = f"{channel}:{near['cluster']}"
