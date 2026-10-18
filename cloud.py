"""Groq cloud backend: Whisper transcription and chat completions over Groq's OpenAI-compatible API.

Errors are split by what the caller should do about them:
  Offline      — no connection / server unreachable: queue the work and retry later
  RateLimited  — free-tier limit hit: wait `retry_after` seconds
  AuthError    — no key, or the key was rejected: keep the work queued until the user fixes it
  CloudError   — anything else (bad request): report it
"""
import io
import json
import re
import time
import wave
from pathlib import Path

import numpy as np
import requests

import securestore

API = "https://api.groq.com/openai/v1"
TRANSCRIBE_MODEL = "whisper-large-v3"
CHAT_MODELS = {                      # id -> label shown in Settings
    "qwen/qwen3.8-27b": "Qwen 3.8 27B — best for Hindi/Hinglish",
    "openai/gpt-oss-120b": "GPT-OSS 120B — strongest reasoning",
    "openai/gpt-oss-20b": "GPT-OSS 20B — fastest",
}
MAX_UPLOAD_S = 600                   # 25 MB free-tier upload limit = ~13 min of 16 kHz mono WAV; stay well under
TIMEOUT = (10, 120)
# Free tier, per chat model and minute: 8,000 tokens in total, so one request must fit in it. Some models also
# cap output tokens per minute: a request that may produce more is refused outright (retrying never helps).
TOKENS_PER_MINUTE = 8000
MAX_OUTPUT_TOKENS = 2000
OUTPUT_LIMITS = {"qwen/qwen3.8-27b": 1000}


def max_output(model: str) -> int:
    return OUTPUT_LIMITS.get(model, MAX_OUTPUT_TOKENS)


class CloudError(Exception):
    pass


class Offline(CloudError):
    pass


class AuthError(CloudError):
    pass


class RateLimited(CloudError):
    def __init__(self, retry_after: float, message: str = ""):
        super().__init__(message or f"rate limited, retry in {retry_after:.0f}s")
        self.retry_after = retry_after


# --- API key, stored DPAPI-encrypted next to the database key ---------------------
class KeyStore:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "groq.key"
        self.import_path = data_dir / "groq-key.txt"   # plain-text drop-in, imported and deleted on start

    def import_plaintext(self) -> bool:
        if not self.import_path.exists():
            return False
        key = self.import_path.read_text(encoding="utf-8-sig").strip()
        self.import_path.unlink()
        if key:
            self.set(key)
        return bool(key)

    def get(self) -> str | None:
        try:
            return securestore.unprotect(self.path.read_bytes()).decode()
        except (OSError, ValueError):
            return None

    def set(self, key: str):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(securestore.protect(key.strip().encode()))

    def clear(self):
        self.path.unlink(missing_ok=True)

    def hint(self) -> str | None:
        k = self.get()
        return f"…{k[-4:]}" if k else None


class Groq:
    def __init__(self, keys: KeyStore):
        self.keys = keys

    def _headers(self) -> dict:
        key = self.keys.get()
        if not key:
            raise AuthError("No Groq API key — add one in Settings → Cloud AI.")
        return {"Authorization": f"Bearer {key}"}

    def _check(self, resp: requests.Response):
        if resp.status_code == 429:
            msg = _error_message(resp)
            if "too large" in msg.lower():       # bigger than a whole minute's allowance: waiting won't help
                raise CloudError(f"Request too large for Groq's free tier: {msg[:200]}")
            raise RateLimited(_retry_after(resp, msg))
        if resp.status_code in (500, 502, 503, 504):
            raise Offline(f"Groq is temporarily unavailable ({resp.status_code})")
        if resp.status_code == 401:
            raise AuthError("Groq rejected the API key — check it in Settings → Cloud AI.")
        if resp.status_code >= 400:
            raise CloudError(f"Groq error {resp.status_code}: {_error_message(resp)[:300]}")

    def _post(self, path: str, **kw) -> requests.Response:
        try:
            resp = requests.post(f"{API}{path}", headers=self._headers(), timeout=TIMEOUT, **kw)
        except (requests.ConnectionError, requests.Timeout) as e:
            raise Offline(f"No connection to Groq ({type(e).__name__})") from e
        self._check(resp)
        return resp

    # --- speech-to-text ----------------------------------------------------------
    def transcribe(self, audio: np.ndarray, language: str | None = None, prompt: str | None = None) -> list[dict]:
        """16 kHz float32 mono -> [{start, end, text, no_speech_prob, avg_logprob}], times in seconds."""
        out = []
        step = MAX_UPLOAD_S * 16000
        for off in range(0, len(audio), step):
            piece = audio[off:off + step]
            data = {"model": TRANSCRIBE_MODEL, "response_format": "verbose_json", "temperature": "0"}
            if language:
                data["language"] = language
            if prompt:
                data["prompt"] = prompt[-800:]          # Whisper prompt is limited to ~224 tokens
            resp = self._post("/audio/transcriptions", data=data,
                              files={"file": ("chunk.wav", _wav_bytes(piece), "audio/wav")})
            for s in resp.json().get("segments", []):
                out.append({"start": off / 16000 + s["start"], "end": off / 16000 + s["end"],
                            "text": s.get("text", ""), "no_speech_prob": s.get("no_speech_prob", 0.0),
                            "avg_logprob": s.get("avg_logprob", 0.0)})
        return out

    # --- chat ------------------------------------------------------------------------
    def chat(self, model: str, messages: list[dict], on_token=None, temperature: float = 0.2,
             max_tokens: int | None = None) -> str:
        body = {"model": model, "messages": messages, "stream": True, "temperature": temperature,
                "max_tokens": min(max_tokens or MAX_OUTPUT_TOKENS, max_output(model))}
        if model.startswith("openai/gpt-oss"):
            body.update(include_reasoning=False, reasoning_effort="low")
        elif model.startswith("qwen/"):
            body["reasoning_effort"] = "none"           # answer straight away; no <think> block
        resp = self._post("/chat/completions", json=body, stream=True)
        out = []
        try:
            for raw in resp.iter_lines():
                if not raw or not raw.startswith(b"data:"):
                    continue
                payload = raw[5:].strip()
                if payload == b"[DONE]":
                    break
                chunk = json.loads(payload)
                if "error" in chunk:
                    raise CloudError(str(chunk["error"]))
                piece = (chunk.get("choices") or [{}])[0].get("delta", {}).get("content") or ""
                if piece:
                    out.append(piece)
                    if on_token:
                        on_token(piece)
        except (requests.ConnectionError, requests.Timeout) as e:
            raise Offline("Connection dropped while Groq was writing") from e
        return "".join(out)

    def ping(self) -> dict:
        """Cheap check that the key works: lists models."""
        try:
            resp = requests.get(f"{API}/models", headers=self._headers(), timeout=(10, 20))
        except (requests.ConnectionError, requests.Timeout) as e:
            raise Offline("No connection to Groq") from e
        self._check(resp)
        ids = {m["id"] for m in resp.json().get("data", [])}
        return {"ok": True, "transcribe": TRANSCRIBE_MODEL in ids,
                "chat_models": [m for m in CHAT_MODELS if m in ids]}


def with_retries(fn, attempts: int = 3, on_wait=None, max_wait: float = 120):
    """Retry a cloud call through rate-limit waits of up to max_wait; Offline and other errors propagate."""
    for i in range(attempts):
        try:
            return fn()
        except RateLimited as e:
            if i == attempts - 1 or e.retry_after > max_wait:
                raise
            if on_wait:
                on_wait(e.retry_after)
            time.sleep(e.retry_after + 0.5)


def _error_message(resp) -> str:
    try:
        return str(resp.json().get("error", {}).get("message") or resp.text)
    except (ValueError, AttributeError):
        return resp.text or ""


def _retry_after(resp, msg: str) -> float:
    """Seconds to wait: the retry-after header, else "Please try again in 1m2.5s" / "in 850ms" in the message."""
    retry = resp.headers.get("retry-after")
    if retry and retry.replace(".", "", 1).isdigit():
        return float(retry)
    m = re.search(r"try again in (?:(\d+)m)?([\d.]+)(ms|s)", msg)
    if m:
        secs = float(m.group(2)) / (1000 if m.group(3) == "ms" else 1)
        return int(m.group(1) or 0) * 60 + secs
    return 30.0


def _wav_bytes(audio: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
    return buf.getvalue()
