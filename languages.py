"""Meeting languages: which speech models to use, what to tell Whisper, and how to word the notes.

Whisper's auto-detection is unreliable on short chunks of Indian-accented or code-switched speech
(it guesses Spanish or French on Hinglish), so an explicit meeting language picks both the models
and the language hint.
"""
import os
from pathlib import Path

LOCAL_MODELS = Path(os.environ.get("APPDATA", Path.home())) / "LocalNotes" / "models"
# Oriserve Whisper-Hindi2Hinglish (Apache-2.0), converted by tools/build_hinglish_model.ps1
LOCAL_MODEL_DIRS = {
    "hinglish-apex": LOCAL_MODELS / "whisper-hinglish-apex",     # large-v3-turbo fine-tune: accuracy pass
    "hinglish-swift": LOCAL_MODELS / "whisper-hinglish-swift",   # base fine-tune: live transcript
}

MEETING_LANGUAGES = {
    "auto": "Auto-detect",
    "en": "English",
    "hi": "Hindi",
    "hinglish": "Hinglish (Hindi + English)",
}
NOTES_LANGUAGES = {
    "same": "Same as the meeting",
    "en": "English",
    "hi": "Hindi",
    "hinglish": "Hinglish",
}
_NOTES_INSTRUCTION = {
    "en": "Write everything in English, translating anything said in other languages.",
    "hi": "Write everything in Hindi, in Devanagari script. Keep product names and technical terms as spoken.",
    "hinglish": ("Write in Hinglish — the natural Hindi-English mix the speakers use — in Roman script "
                 "(e.g. \"Launch ko March 15 tak shift karenge\"), not Devanagari."),
}


def local_model_available(name: str) -> bool:
    d = LOCAL_MODEL_DIRS.get(name)
    return bool(d and (d / "model.bin").exists())


def _multilingual(model: str) -> str:
    """English-only Whisper checkpoints can't hear other languages; use their multilingual twin."""
    if model == "distil-large-v3":           # English-only, no multilingual distil equivalent
        return "large-v3-turbo"
    return model[:-3] if model.endswith(".en") else model


def speech_plan(language: str, live_model: str, refine_model: str) -> dict:
    """-> {live, live_language, refine, refine_language}; refine is "off" when the accuracy pass is disabled.

    *_language is the code passed to Whisper (None = auto-detect).
    """
    language = language if language in MEETING_LANGUAGES else "auto"
    if language == "en":
        return {"live": live_model, "live_language": "en", "refine": refine_model, "refine_language": "en"}
    live = _multilingual(live_model)
    refine = refine_model if refine_model == "off" else _multilingual(refine_model)
    hint = None if language == "auto" else "hi" if language == "hinglish" else language
    plan = {"live": live, "live_language": hint, "refine": refine, "refine_language": hint}
    if language == "hinglish":
        # Oriserve's Hinglish models were fine-tuned with the English language token and write Roman Hinglish.
        if local_model_available("hinglish-swift"):
            plan.update(live="hinglish-swift", live_language="en")
        if refine != "off" and local_model_available("hinglish-apex"):
            plan.update(refine="hinglish-apex", refine_language="en")
    return plan


def notes_instruction(notes_language: str, meeting_language: str) -> str:
    target = meeting_language if notes_language == "same" else notes_language
    if target in _NOTES_INSTRUCTION:
        return _NOTES_INSTRUCTION[target]
    return "Write in the main language of the meeting."
