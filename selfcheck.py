"""Verify a LocalNotes install: python selfcheck.py  (exit code 1 if something essential is missing)."""
import importlib
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
results: list[tuple[str, bool, str, bool]] = []   # (name, ok, detail, essential)


def check(name: str, essential: bool = True):
    def wrap(fn):
        try:
            detail = fn() or ""
            results.append((name, True, detail, essential))
        except Exception as e:
            results.append((name, False, f"{type(e).__name__}: {e}", essential))
        return fn
    return wrap


@check("Python version")
def _():
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError(f"need 3.12, found {sys.version.split()[0]}")
    return sys.version.split()[0]


@check("Python packages")
def _():
    for mod in ("webview", "pyaudiowpatch", "faster_whisper", "soxr", "numpy", "requests",
                "pystray", "PIL", "sherpa_onnx", "sqlcipher3"):
        importlib.import_module(mod)
    return "all importable"


@check("App modules")
def _():
    for mod in ("app", "audio", "db", "detector", "hotkeys", "securestore", "speakers",
                "summarize", "templates", "transcribe", "tray"):
        importlib.import_module(mod)
    return "all importable"


@check("Encrypted database (SQLCipher + full-text search)")
def _():
    from db import DB
    d = DB(Path(tempfile.mkdtemp()) / "check.db", os.urandom(32))
    mid = d.create_meeting("Selfcheck budget review")
    d.reindex(mid)
    assert d.search("budg"), "search returned nothing"
    return "AES-256, FTS5 ok"


@check("Windows key protection (DPAPI)")
def _():
    import securestore
    assert securestore.unprotect(securestore.protect(b"k" * 32)) == b"k" * 32
    return "round-trip ok"


@check("Audio devices")
def _():
    import pyaudiowpatch as pa
    p = pa.PyAudio()
    try:
        w = p.get_host_api_info_by_type(pa.paWASAPI)
        mic = p.get_device_info_by_index(w["defaultInputDevice"])["name"]
        loop = p.get_default_wasapi_loopback()["name"]
    finally:
        p.terminate()
    return f"mic: {mic} | system audio: {loop}"


@check("Speaker-recognition models", essential=False)
def _():
    import speakers
    if not speakers.available():
        raise RuntimeError(f"missing in {speakers.MODELS_DIR} — run setup.ps1")
    return str(speakers.MODELS_DIR)


@check("Hinglish speech models", essential=False)
def _():
    import languages
    missing = [n for n in languages.LOCAL_MODEL_DIRS if not languages.local_model_available(n)]
    if missing:
        raise RuntimeError(f"not built ({', '.join(missing)}) — run tools\\build_hinglish_model.ps1")
    return "Swift (live) + Apex (accuracy pass)"


@check("Ollama + AI model", essential=False)
def _():
    import app
    import summarize
    model = app.load_settings()["ollama_model"]
    st = summarize.ollama_status(model)
    if not st["running"]:
        raise RuntimeError("Ollama isn't running — start the Ollama app")
    if not st["model_ready"]:
        raise RuntimeError(f"model missing — run: ollama pull {model}")
    return model


if __name__ == "__main__":
    width = max(len(r[0]) for r in results)
    for name, ok, detail, essential in results:
        mark = "OK  " if ok else ("FAIL" if essential else "WARN")
        print(f"  [{mark}] {name.ljust(width)}  {detail}")
    failed = [r for r in results if not r[1] and r[3]]
    print("\nLocalNotes is ready." if not failed else f"\n{len(failed)} essential check(s) failed.")
    sys.exit(1 if failed else 0)
