"""LocalNotes: a meeting recorder, transcriber and note-taker.

Audio is recorded and stored on this PC. Speech-to-text and notes run on Groq (cloud mode, the default) or
entirely locally with faster-whisper and Ollama (local mode).
"""
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import winreg
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

import webview

import cloud
import languages
import securestore
import speakers
import summarize
import templates
from audio import CLOUD_CHUNK_S, Recorder
from db import DB, is_plain
from detector import MeetingDetector
from hotkeys import Hotkeys
from transcribe import Transcriber, _read_wav, cloud_fill, refine
from tray import Tray

__version__ = "0.1.0"

APP_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("LOCALNOTES_DATA") or Path(os.environ.get("APPDATA", Path.home())) / "LocalNotes")
SETTINGS_PATH = DATA_DIR / "settings.json"
AUDIO_DIR = DATA_DIR / "audio"
UI_DIR = APP_DIR / "ui"
ICON_PATH = APP_DIR / "assets" / "localnotes.ico"
APP_USER_MODEL_ID = "VarunMundhada.LocalNotes"   # own taskbar identity instead of grouping under Python
STARTUP_LNK = Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs\Startup\LocalNotes.lnk"
TASK_RE = re.compile(r"^(\s*[-*]\s+\[)( |x|X)(\]\s+)(.*)$")
_user32 = ctypes.windll.user32

DEFAULT_SETTINGS = {
    "engine": "cloud",              # cloud: Groq does speech-to-text and notes / local: faster-whisper + Ollama
    "cloud_model": "qwen/qwen3.8-27b",   # Groq chat model for notes, Ask and follow-ups (cloud.CHAT_MODELS)
    "whisper_model": "base.en",     # live pass: tiny.en / base.en / small.en (multilingual twin used for non-English)
    "refine_model": "large-v3-turbo",  # after-meeting pass; "off" to skip
    "meeting_language": "en",       # default for new meetings: auto / en / hi / hinglish (see languages.py)
    "notes_language": "same",       # same / en / hi / hinglish
    "vocabulary": "",               # names / jargon to spell correctly, comma or newline separated
    "keep_audio": False,            # keep the WAVs after the refine pass
    "ollama_model": "qwen3:4b-instruct",   # writes Hindi/Hinglish well; non-thinking, so it answers straight away
    "settings_version": 2,
    "detect_meetings": True,
    "auto_record": False,           # start recording without asking when a call is detected
    "auto_stop": True,              # stop when the detected call ends
    "auto_notes": True,             # generate AI notes as soon as recording stops
    "capture_system": True,
    "capture_mic": True,
    "show_widget": True,            # floating widget for detected calls / recording
    "close_to_tray": True,          # closing the window keeps LocalNotes running in the tray
    "launch_at_login": False,
    "theme": "system",              # system / light / dark
    "ui_style": "glass",            # glass / classic
    "identify_speakers": True,      # split voices after the call and recognise saved voice profiles
    "your_name": os.environ.get("USERNAME", "").split(".")[0].title(),
    "default_template": "auto",     # "auto" = guess from the meeting title, else a template id
    "followup_tone": "friendly",
    "hotkey_record": "ctrl+alt+n",  # start / stop recording from anywhere
    "hotkey_pause": "ctrl+alt+p",   # pause / resume
}
LIVE_ID_THRESHOLD = 0.68            # stricter than post-call matching: a live chunk may mix voices
MIN_CLUSTER_S = 3.0                 # voice clusters shorter than this are folded into the channel's main voice
CATCHUP_EVERY_S = 30.0              # how often queued meetings retry the cloud; backs off to CATCHUP_MAX_S
CATCHUP_MAX_S = 600.0
CLOUD_WAIT_ERRORS = (cloud.Offline, cloud.RateLimited, cloud.AuthError)   # work stays queued; retried later
NOTES_REQUESTED = 2                 # meetings.cloud_pending: notes were asked for while the cloud was unreachable


def error_text(e: Exception) -> str:
    """An AI request that failed now (Ask, follow-up), in words for the UI."""
    if isinstance(e, cloud.RateLimited):
        return "Groq's free-tier limit was reached — try again in a minute."
    if isinstance(e, cloud.Offline):
        return "No internet connection — try again when you're back online."
    return str(e)


def cloud_reason(e: Exception) -> str:
    """Why cloud work is waiting, in words for the UI."""
    if isinstance(e, cloud.AuthError):
        return str(e)
    if isinstance(e, cloud.RateLimited):
        return "Groq's free-tier limit was reached — LocalNotes will finish this automatically shortly."
    return "No internet connection — LocalNotes will finish this automatically when you're back online."


class DataLockedError(Exception):
    """The encrypted database can't be opened with this Windows account's key."""


def open_database() -> DB:
    db_path, key_path = DATA_DIR / "localnotes.db", DATA_DIR / "storage.key"
    locked_help = (f"Your notes are untouched in:\n{DATA_DIR}\n\n"
                   "If you have a backup of storage.key from this Windows account, put it back in that folder. "
                   "To start fresh instead, move localnotes.db somewhere else and open LocalNotes again.")
    # Never mint a new key while an encrypted database exists: it could never open that data,
    # and it would sit where a restored backup of the real key has to go.
    if not key_path.exists() and db_path.exists() and not is_plain(db_path):
        raise DataLockedError("LocalNotes can't find its encryption key (storage.key).\n\n" + locked_help)
    try:
        key = securestore.load_or_create_key(key_path)
    except OSError as e:
        raise DataLockedError(f"LocalNotes can't unlock its encryption key ({e}). This usually means the data "
                              "folder was copied from another Windows account or PC.\n\n" + locked_help) from e
    try:
        return DB(db_path, key)
    except Exception as e:
        raise DataLockedError(f"LocalNotes can't open its notes database ({e}).\n\n" + locked_help) from e


def load_settings() -> dict:
    try:
        saved = json.loads(SETTINGS_PATH.read_text())
    except Exception:
        return dict(DEFAULT_SETTINGS)
    if saved.get("settings_version", 1) < 2:
        # v2: the old default notes model (English-only in practice) becomes the multilingual one,
        # unless the user had picked a different model on purpose.
        if saved.get("ollama_model") == "llama3.2:3b":
            saved["ollama_model"] = DEFAULT_SETTINGS["ollama_model"]
        saved["settings_version"] = 2
    return {**DEFAULT_SETTINGS, **saved}


def system_prefers_dark() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def first_line_summary(md: str) -> str:
    for line in md.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or TASK_RE.match(line):
            continue
        s = re.sub(r"^[-*•]\s+|\*\*|__|`", "", s)
        s = re.sub(r"^(summary|one-line summary)\s*:\s*", "", s, flags=re.I)
        if s:
            return s[:180]
    return ""


class Api:
    """Methods here are callable from the UI as window.pywebview.api.<name>()."""

    def __init__(self):
        self._settings = load_settings()
        self._db = open_database()
        self._db.sync_builtin_templates(templates.BUILTIN)
        self._hotkeys = Hotkeys()
        self._paused_at: float | None = None
        self._paused_total = 0.0
        self._busy_followup: set[int] = set()
        self._main = None
        self._pill = None
        self._pill_visible = False
        self._pill_lock = threading.Lock()
        self._tray = None
        self._recorder: Recorder | None = None
        self._meeting_id: int | None = None
        self._meeting_app: str | None = None
        self._rec_started: float | None = None
        self._rec_title = ""
        self._detected: tuple[str, str] | None = None
        self._pipeline: dict[int, str] = {}
        self._busy_notes: set[int] = set()
        self._quitting = False
        self._tray_hint_shown = False
        self._speakers = speakers.SpeakerEngine() if speakers.available() else None
        self._voice_cache: dict[str, dict] | None = None
        self._transcriber = Transcriber(self._settings["whisper_model"], self._on_segment, self._status,
                                        identify=self._identify_live)
        self._transcriber.vocabulary = self._settings["vocabulary"]
        self._refine_lock = threading.Lock()
        self._detector = MeetingDetector(self._on_meeting_start, self._on_meeting_end)
        self._detector.enabled = self._settings["detect_meetings"]
        # Cloud: the Groq key lives DPAPI-encrypted next to the database key. A plain-text groq-key.txt
        # dropped into the data folder is imported once and deleted.
        self._keys = cloud.KeyStore(DATA_DIR)
        try:
            self._keys.import_plaintext()
        except OSError:
            pass
        self._groq = cloud.Groq(self._keys)
        self._cloud_issue = ""                 # why the cloud is unusable right now ("" = fine)
        self._cloud_lock = threading.Lock()    # one catch-up at a time
        self._catchup_wake = threading.Event()
        self._transcriber.on_gap = self._on_gap
        self._transcriber.on_cloud = self._on_cloud
        self._apply_engine()
        threading.Thread(target=self._catchup_loop, daemon=True).start()

    # --- plumbing -------------------------------------------------------
    def _emit(self, kind: str, **data):
        if self._main:
            payload = json.dumps({"type": kind, **data})
            try:
                self._main.evaluate_js(f"window.onEvent && window.onEvent({payload})")
            except Exception:
                pass

    def _status(self, text: str):
        self._emit("status", text=text)

    def _on_segment(self, mid, speaker, t0, t1, text, channel=None):
        seg = self._db.add_segment(mid, speaker, t0, t1, text, channel)
        self._emit("segment", meeting_id=mid, segment=seg)

    def _on_chunk(self, speaker, t0, audio):
        if self._meeting_id is not None:
            self._transcriber.submit(self._meeting_id, speaker, t0, audio)

    def _stage(self, mid: int, stage: str, **extra):
        if stage in ("done", "waiting"):     # waiting = queued for the cloud; the meeting is usable meanwhile
            self._pipeline.pop(mid, None)
        else:
            self._pipeline[mid] = stage
        self._emit("pipeline", meeting_id=mid, stage=stage, **extra)

    # --- state ----------------------------------------------------------
    def get_state(self):
        return {
            "settings": self._settings,
            "recording": self._recorder is not None,
            "meeting_id": self._meeting_id,
            "recording_started": self._rec_started,
            "paused_at": self._paused_at,
            "paused_total": self._paused_total,
            "encrypted": self._db.encrypted,
            "hotkeys_failed": self._hotkeys.failed,
            "ollama": ({"running": True, "model_ready": True} if self._cloud_mode()
                       else summarize.ollama_status(self._settings["ollama_model"])),
            "cloud": self.cloud_status(),
            "detected": self._detected,
            "user": os.environ.get("USERNAME", "").split(".")[0].title(),
            "version": __version__,
            "languages": languages.MEETING_LANGUAGES,
            "notes_languages": languages.NOTES_LANGUAGES,
            "hinglish_models": languages.local_model_available("hinglish-apex"),
        }

    def levels(self):
        return {
            "levels": self._recorder.levels() if self._recorder else {},
            "pending": self._transcriber.pending(),
        }

    def save_settings(self, new: dict):
        before = dict(self._settings)
        new = {k: v for k, v in new.items() if k in DEFAULT_SETTINGS}
        if new.get("cloud_model", before["cloud_model"]) not in cloud.CHAT_MODELS:
            new.pop("cloud_model", None)
        if new.get("engine", before["engine"]) not in ("cloud", "local"):
            new.pop("engine", None)
        if new.get("hotkey_record", before["hotkey_record"]) == new.get("hotkey_pause", before["hotkey_pause"]):
            new.pop("hotkey_record", None)   # one shortcut can't do both; keep the previous assignment
            new.pop("hotkey_pause", None)
        self._settings.update(new)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(self._settings, indent=2))
        self._detector.enabled = self._settings["detect_meetings"]
        self._transcriber.vocabulary = self._settings["vocabulary"]
        if self._settings["engine"] != before.get("engine"):
            self._apply_engine()
        if self._meeting_id is not None:   # live model/language follow the settings mid-call too
            self._configure_live(self._meeting_language(self._meeting_id))
        if self._settings["launch_at_login"] != before.get("launch_at_login"):
            self._apply_login_item(self._settings["launch_at_login"])
        if (self._settings["hotkey_record"], self._settings["hotkey_pause"]) != \
                (before.get("hotkey_record"), before.get("hotkey_pause")):
            self.start_hotkeys()
        if self._tray:
            self._tray.refresh()
        self._sync_pill()
        return self._settings

    def _apply_login_item(self, enabled: bool):
        if not enabled:
            STARTUP_LNK.unlink(missing_ok=True)
            return
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        ps = (
            "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:LN_LNK);"
            "$s.TargetPath=$env:LN_PY;$s.Arguments=$env:LN_ARGS;$s.WorkingDirectory=$env:LN_DIR;"
            "$s.IconLocation=$env:LN_ICON;$s.Save()"
        )
        env = {**os.environ, "LN_LNK": str(STARTUP_LNK), "LN_PY": str(pythonw),
               "LN_ARGS": f'"{APP_DIR / "app.py"}" --background', "LN_DIR": str(APP_DIR), "LN_ICON": str(ICON_PATH)}
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], env=env,
                       creationflags=subprocess.CREATE_NO_WINDOW, check=False)

    # --- meetings -------------------------------------------------------
    def list_meetings(self):
        rows = self._db.list_meetings()
        for r in rows:
            r["live"] = r["id"] == self._meeting_id
            r["queued"] = self._queued(r["id"], r.pop("cloud_pending"))
        return rows

    def _queued(self, mid: int, pending) -> bool:
        """Waiting for the cloud (not while it's still recording or being worked on)."""
        return bool(pending) and mid != self._meeting_id and mid not in self._pipeline

    def search(self, q: str):
        return self._db.search(q)

    def get_meeting(self, mid: int):
        m = self._db.get_meeting(mid)
        if m:
            m["has_audio"] = bool(self._tracks_for(mid))
            m["pipeline"] = self._pipeline.get(mid)
            m["speakers"] = [{k: s[k] for k in ("cluster", "name", "seconds", "source")}
                             for s in self._db.meeting_speakers(mid)]
            m["live"] = mid == self._meeting_id
            m["queued"] = self._queued(mid, m.pop("cloud_pending", 0))
            m["queued_reason"] = self._cloud_issue if m["queued"] else ""
        return m

    def _tracks_for(self, mid: int) -> dict:
        folder = AUDIO_DIR / str(mid)
        return {p.stem: p for p in folder.glob("*.wav")} if folder.is_dir() else {}

    def update_meeting(self, mid: int, fields: dict):
        self._db.update_meeting(mid, **fields)
        if mid == self._meeting_id and "title" in fields:
            self._rec_title = fields["title"]
        return True

    def delete_meeting(self, mid: int):
        if mid == self._meeting_id or mid in self._pipeline:   # still recording or being processed
            return False
        self._db.delete_meeting(mid)
        shutil.rmtree(AUDIO_DIR / str(mid), ignore_errors=True)
        return True

    def get_home(self):
        now = time.time()
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = (today - timedelta(days=today.weekday())).timestamp()
        rows = self._db.list_meetings_full(200)
        week = [m for m in rows if m["started_at"] >= week_start]
        minutes = sum(((m["ended_at"] or now) - m["started_at"]) / 60 for m in week)
        tasks, open_count = [], 0
        for m in rows[:80]:
            for idx, (done, text) in enumerate(_tasks(m["ai_notes"])):
                if not done:
                    open_count += 1
                    if len(tasks) < 40:
                        tasks.append({"meeting_id": m["id"], "meeting": m["title"], "index": idx,
                                      "text": text, "started_at": m["started_at"]})
        recent = [{"id": m["id"], "title": m["title"], "started_at": m["started_at"],
                   "ended_at": m["ended_at"], "app": m["app"], "summary": first_line_summary(m["ai_notes"]),
                   "live": m["id"] == self._meeting_id} for m in rows[:6]]
        return {"stats": {"week_meetings": len(week), "week_minutes": round(minutes),
                          "open_tasks": open_count, "total": len(rows)},
                "tasks": tasks, "recent": recent}

    def toggle_task(self, mid: int, index: int, done: bool):
        m = self._db.get_meeting(mid)
        if not m:
            return None
        lines, n = m["ai_notes"].split("\n"), -1
        for i, line in enumerate(lines):
            mt = TASK_RE.match(line)
            if mt:
                n += 1
                if n == index:
                    lines[i] = f"{mt.group(1)}{'x' if done else ' '}{mt.group(3)}{mt.group(4)}"
                    break
        notes = "\n".join(lines)
        self._db.update_meeting(mid, ai_notes=notes)
        return notes

    def export_meeting(self, mid: int):
        m = self._db.get_meeting(mid)
        if not m or not self._main:
            return None
        safe = re.sub(r'[\\/:*?"<>|]+', "-", m["title"]).strip() or "Meeting"
        res = self._main.create_file_dialog(webview.SAVE_DIALOG, save_filename=f"{safe}.md",
                                            file_types=("Markdown (*.md)",))
        if not res:
            return None
        path = res if isinstance(res, str) else res[0]
        when = datetime.fromtimestamp(m["started_at"]).strftime("%A %d %B %Y, %H:%M")
        body = [f"# {m['title']}", f"_{when}_", ""]
        if m["ai_notes"].strip():
            body += [m["ai_notes"].strip(), ""]
        if m["user_notes"].strip():
            body += ["## My notes", m["user_notes"].strip(), ""]
        body += ["## Transcript", self._db.transcript_text(mid)]
        Path(path).write_text("\n".join(body), encoding="utf-8")
        return path

    # --- recording ------------------------------------------------------
    def start_recording(self, title: str = "", app: str = "manual"):
        if self._recorder:
            return self._meeting_id
        title = title.strip() or time.strftime("Meeting %d %b, %H:%M")
        mid = self._db.create_meeting(title, app, self._pick_template(title))
        self._db.update_meeting(mid, language=self._settings["meeting_language"])
        self._configure_live(self._settings["meeting_language"])
        cloud_mode = self._cloud_mode()
        if cloud_mode:
            # Marked until the cloud has finished it, so a crash or quit mid-way is picked up on the next start.
            # The audio is always kept until then: stretches the cloud missed are filled in from it.
            self._db.set_cloud_pending(mid, 1)
        save_audio = (cloud_mode or self._settings["refine_model"] != "off" or self._settings["keep_audio"]
                      or (self._settings["identify_speakers"] and self._speakers is not None))
        rec = Recorder(self._on_chunk, self._settings["capture_system"], self._settings["capture_mic"],
                       audio_dir=AUDIO_DIR / str(mid) if save_audio else None,
                       chunk_s=CLOUD_CHUNK_S if cloud_mode else None)
        self._meeting_id, self._meeting_app = mid, app
        try:
            rec.start()
        except Exception as e:
            self._meeting_id = self._meeting_app = None
            self._db.delete_meeting(mid)
            self._status(f"Could not open audio devices: {e}")
            return None
        self._recorder, self._rec_started, self._detected = rec, time.time(), None
        self._rec_title = title
        self._paused_at, self._paused_total = None, 0.0
        self._emit("recording", recording=True, meeting_id=mid, devices=rec.devices, started=self._rec_started)
        if self._tray:
            self._tray.refresh()
        self._sync_pill()
        return mid

    def stop_recording(self):
        rec, mid = self._recorder, self._meeting_id
        if not rec:
            return None
        self._recorder, self._rec_started = None, None
        self._paused_at, self._paused_total = None, 0.0
        rec.stop()                     # flushes the last chunks into the transcriber queue
        # Release "the current meeting" now, not when processing finishes: a new recording may
        # start while this one is still being processed, and must not be cleared by it.
        self._pipeline[mid] = "transcribing"   # claimed before the catch-up worker could see it as queued
        self._meeting_id = self._meeting_app = None
        self._emit("recording", recording=False, meeting_id=mid)
        if self._tray:
            self._tray.refresh()
        self._sync_pill()
        threading.Thread(target=self._finish, args=(mid,), daemon=True).start()
        return mid

    def pause_recording(self):
        if not self._recorder or self._paused_at:
            return False
        self._recorder.pause()
        self._paused_at = time.time()
        self._emit("paused", paused=True, meeting_id=self._meeting_id,
                   paused_at=self._paused_at, paused_total=self._paused_total)
        return True

    def resume_recording(self):
        if not self._recorder or not self._paused_at:
            return False
        self._paused_total += time.time() - self._paused_at
        self._paused_at = None
        self._recorder.resume()
        self._emit("paused", paused=False, meeting_id=self._meeting_id,
                   paused_at=None, paused_total=self._paused_total)
        return True

    def toggle_pause(self):
        return self.resume_recording() if self._paused_at else self.pause_recording()

    def _elapsed(self) -> float:
        if not self._rec_started:
            return 0.0
        paused = self._paused_total + (time.time() - self._paused_at if self._paused_at else 0.0)
        return time.time() - self._rec_started - paused

    # --- global hotkeys ---------------------------------------------------
    def start_hotkeys(self):
        self._hotkeys.start({
            self._settings["hotkey_record"]: self._hotkey_record,
            self._settings["hotkey_pause"]: self.toggle_pause,
        })
        if self._hotkeys.failed:
            self._emit("hotkey_failed", combos=self._hotkeys.failed)

    def _hotkey_record(self):
        if self._recorder:
            self.stop_recording()
            if self._tray:
                self._tray.notify("Recording stopped", "Writing your notes…")
            return
        mid = self.start_from_detection() if self._detected else self.start_recording()
        if mid and self._tray:
            self._tray.notify("Recording started", f"Press {self._settings['hotkey_record'].upper()} again to stop.")

    def _pick_template(self, title: str) -> str:
        choice = self._settings["default_template"]
        if choice != "auto" and self._db.template(choice):
            return choice
        return templates.suggest(title)

    def _finish(self, mid):
        self._stage(mid, "transcribing")
        self._transcriber.wait_idle()
        self._db.end_meeting(mid)
        self._db.reindex(mid)
        # Who spoke first (so notes use names), then quick notes from the live transcript,
        # then the slower accurate pass, then rewrite the notes from the better transcript.
        if self._settings["identify_speakers"] and self._speakers and self._tracks_for(mid):
            self._stage(mid, "speakers")
            self._identify_meeting(mid)
        if self._db.get_meeting(mid)["cloud_pending"]:   # recorded in cloud mode
            self._cloud_complete(mid)
            return
        m = self._db.get_meeting(mid)
        empty = not m["segments"] and not m["user_notes"].strip()
        if self._settings["auto_notes"] and not empty:
            self._stage(mid, "notes")
            self._run_notes(mid)
        if self._settings["refine_model"] != "off" and self._tracks_for(mid) and not empty:
            self._stage(mid, "refining")
            ok = self._refine_worker(mid)
            if not self._settings["keep_audio"]:
                shutil.rmtree(AUDIO_DIR / str(mid), ignore_errors=True)
            if ok and self._settings["auto_notes"]:
                self._stage(mid, "renotes")
                self._run_notes(mid)
        elif not self._settings["keep_audio"]:
            shutil.rmtree(AUDIO_DIR / str(mid), ignore_errors=True)
        self._stage(mid, "done")

    # --- cloud (Groq) -----------------------------------------------------------
    def _cloud_mode(self) -> bool:
        return self._settings["engine"] == "cloud"

    def _notes_model(self) -> str:
        return self._settings["cloud_model"] if self._cloud_mode() else self._settings["ollama_model"]

    def _apply_engine(self):
        on = self._cloud_mode()
        self._transcriber.cloud = self._groq if on else None
        summarize.use_cloud(self._groq)       # cloud models route there; Ollama models never do
        if on:
            self._transcriber.unload()        # free the local Whisper model's memory
            self._catchup_wake.set()

    def _on_gap(self, mid, channel, t0, t1):
        self._db.add_gap(mid, channel, t0, t1)

    def _on_cloud(self, ok: bool, reason: str):
        self._cloud_issue = "" if ok else reason
        self._emit("cloud", ok=ok, reason=reason)
        if ok:
            self._catchup_wake.set()

    def _fill_gaps(self, mid: int):
        """Transcribe what the live pass couldn't send, from the saved audio. Raises CLOUD_WAIT_ERRORS if it can't."""
        gaps = self._db.gaps(mid)
        if not gaps:
            return
        tracks = self._tracks_for(mid)
        if tracks:
            self._stage(mid, "catchup")
            plan = languages.cloud_plan(self._meeting_language(mid))
            segs = cloud_fill(self._groq, tracks, gaps, plan["language"], self._settings["vocabulary"],
                              on_progress=lambda s: self._emit("catchup_progress", meeting_id=mid, text=s),
                              seed=plan["seed"])
            for s in segs:
                self._db.add_segment(mid, s["speaker"], s["t_start"], s["t_end"], s["text"], s["channel"])
            self._apply_speaker_labels(mid)    # name the new lines like the rest
            self._db.reindex(mid)
        self._db.clear_gaps(mid)               # (without audio there is nothing left to fill)

    def _cloud_complete(self, mid: int) -> bool:
        """Finish a cloud-mode meeting: fill transcript gaps, then write the notes.

        Offline / rate-limited / no key: the meeting stays queued (audio kept) and the catch-up worker
        retries it. Returns True when the meeting is finished.
        """
        with self._cloud_lock:
            if not self._db.get_meeting(mid):  # deleted while queued
                return True
            try:
                self._fill_gaps(mid)
                m = self._db.get_meeting(mid)
                wanted = self._settings["auto_notes"] or m["cloud_pending"] == NOTES_REQUESTED
                if (m["segments"] or m["user_notes"].strip()) and wanted:
                    self._stage(mid, "notes")
                    self._busy_notes.add(mid)
                    try:
                        self._write_notes(mid)
                    finally:
                        self._busy_notes.discard(mid)
            except CLOUD_WAIT_ERRORS as e:
                self._db.set_cloud_pending(mid, max(1, self._db.get_meeting(mid)["cloud_pending"]))
                self._cloud_issue = cloud_reason(e)
                self._emit("notes_queued", meeting_id=mid, reason=self._cloud_issue)
                self._stage(mid, "waiting", reason=self._cloud_issue)
                return False
            except Exception as e:             # a real error: report it, don't retry forever
                self._emit("notes_error", meeting_id=mid, error=str(e))
            self._cloud_done(mid)
            self._stage(mid, "done")
            return True

    def _cloud_done(self, mid: int):
        self._db.set_cloud_pending(mid, 0)
        if not self._settings["keep_audio"] and not self._db.gaps(mid):
            shutil.rmtree(AUDIO_DIR / str(mid), ignore_errors=True)

    def _catchup_loop(self):
        """Finish meetings queued for the cloud once it's reachable: on start, every so often, and on reconnect."""
        delay = 5.0
        while not self._quitting:
            self._catchup_wake.wait(delay)
            self._catchup_wake.clear()
            delay = self._catchup_once(delay)

    def _catchup_once(self, delay: float) -> float:
        """One pass over the queue. Returns how long to wait before the next one (backs off while offline)."""
        queued = [mid for mid in self._db.cloud_pending_meetings()
                  if mid != self._meeting_id and mid not in self._pipeline]
        if not queued or not self._cloud_mode() or not self._keys.get():   # on-device mode never calls out
            return CATCHUP_EVERY_S
        try:
            self._groq.ping()
        except cloud.CloudError as e:
            self._cloud_issue = cloud_reason(e) if isinstance(e, CLOUD_WAIT_ERRORS) else str(e)
            return min(delay * 2, CATCHUP_MAX_S)
        self._cloud_issue = ""
        self._transcriber.cloud_reachable()
        for mid in queued:
            if self._quitting or not self._cloud_complete(mid):
                break
        return CATCHUP_EVERY_S

    def cloud_status(self):
        return {"has_key": bool(self._keys.get()), "hint": self._keys.hint(), "issue": self._cloud_issue,
                "models": cloud.CHAT_MODELS,
                "queued": sum(self._queued(mid, 1) for mid in self._db.cloud_pending_meetings())}

    def set_cloud_key(self, key: str):
        """Save a Groq API key (encrypted) and check it. The key itself is never sent back to the UI."""
        key = (key or "").strip()
        if not key.startswith("gsk_") or len(key) < 20 or any(c.isspace() for c in key):
            return {"ok": False, "saved": False, "error": "That doesn't look like a Groq API key — it starts with gsk_."}
        old = self._keys.get()
        self._keys.set(key)
        result = self.test_cloud()
        saved = result["ok"] or not result.get("bad_key")   # offline: keep it, it's checked again later
        if not saved:
            if old:
                self._keys.set(old)
            else:
                self._keys.clear()
        return {**result, "saved": saved, **self.cloud_status()}

    def remove_cloud_key(self):
        self._keys.clear()
        return self.cloud_status()

    def test_cloud(self):
        try:
            info = self._groq.ping()
        except cloud.AuthError as e:
            return {"ok": False, "error": str(e), "bad_key": True}
        except cloud.CloudError as e:
            return {"ok": False, "error": cloud_reason(e) if isinstance(e, cloud.Offline) else str(e)}
        self._cloud_issue = ""
        self._transcriber.cloud_reachable()
        self._catchup_wake.set()
        return {"ok": True, "transcribe": info["transcribe"], "chat_models": info["chat_models"]}

    # --- speakers & voice profiles ----------------------------------------
    def _profiles(self, include_self: bool = False) -> dict:
        if self._voice_cache is None:
            self._voice_cache = self._db.voices()
        return {n: v["embedding"] for n, v in self._voice_cache.items() if include_self or not v["is_self"]}

    def _identify_live(self, channel: str, audio):
        """Name a live 'Them' chunk if it clearly matches a saved voice."""
        if channel != "Them" or not self._speakers or not self._settings["identify_speakers"]:
            return None
        profiles = self._profiles()
        if not profiles:
            return None
        name, score = speakers.best_match(self._speakers.embed(audio), profiles)
        return name if name and score >= LIVE_ID_THRESHOLD else None

    def _learn_voice(self, name: str, fp, is_self: bool = False):
        cur = self._db.voices().get(name)
        emb, n = speakers.merge_profile(cur["embedding"] if cur else None, cur["samples"] if cur else 0, fp)
        self._db.save_voice(name, emb, n, is_self)
        self._voice_cache = None

    def _identify_meeting(self, mid: int):
        """Split each channel into voices, name them (you / saved profiles / Speaker N) and label the transcript."""
        try:
            tracks = self._tracks_for(mid)
            profiles = self._profiles()
            all_turns, found = [], []    # found: {cluster, channel, seconds, fp}
            for channel in ("Me", "Them"):
                if channel not in tracks:
                    continue
                self._emit("speakers_progress", meeting_id=mid, text=f"Listening for voices ({channel})…")
                turns, fps = self._speakers.diarize(_read_wav(tracks[channel]))
                secs: dict[int, float] = {}
                for t in turns:
                    t["channel"] = channel
                    secs[t["cluster"]] = secs.get(t["cluster"], 0.0) + t["t_end"] - t["t_start"]
                if not secs:
                    continue
                main = max(secs, key=secs.get)
                for t in turns:   # fold tiny clusters (coughs, crosstalk) into the channel's main voice
                    if secs[t["cluster"]] < MIN_CLUSTER_S:
                        t["cluster"] = main
                all_turns += turns
                for c in {t["cluster"] for t in turns}:
                    found.append({"cluster": f"{channel}:{c}", "channel": channel, "is_main": c == main,
                                  "seconds": secs[c], "fp": fps.get(c)})

            # The dominant voice on your mic is you; everyone else is matched against saved profiles
            # one-to-one (best score first), and the rest become Speaker 1, 2, ...
            names: dict[str, tuple[str, str]] = {}
            for f in found:
                if f["channel"] == "Me" and f["is_main"]:
                    names[f["cluster"]] = ("Me", "you")
                    if f["fp"] is not None and self._settings["your_name"]:
                        self._learn_voice(self._settings["your_name"], f["fp"], is_self=True)
            candidates = sorted(
                ((float(f["fp"] @ p), f["cluster"], n) for f in found if f["cluster"] not in names and f["fp"] is not None
                 for n, p in profiles.items()), reverse=True)
            taken = set()
            for score, cluster, name in candidates:
                if cluster in names or name in taken or score < speakers.MATCH_THRESHOLD:
                    continue
                names[cluster] = (name, "profile")
                taken.add(name)
                if score >= 0.7:   # confident match: let the profile keep adapting to this voice
                    self._learn_voice(name, next(f["fp"] for f in found if f["cluster"] == cluster))
            n = 0
            for f in sorted(found, key=lambda f: -f["seconds"]):
                if f["cluster"] not in names:
                    n += 1
                    names[f["cluster"]] = (f"Speaker {n}", "auto")

            self._db.save_speaker_analysis(mid, all_turns, [
                {"cluster": f["cluster"], "name": names[f["cluster"]][0], "embedding": f["fp"],
                 "seconds": f["seconds"], "source": names[f["cluster"]][1]} for f in found])
            self._apply_speaker_labels(mid)
            known = [nm for nm, src in names.values() if src == "profile"]
            self._emit("speakers_done", meeting_id=mid, count=len(found), recognised=known)
        except Exception as e:
            self._emit("speakers_error", meeting_id=mid, error=str(e))

    def _apply_speaker_labels(self, mid: int):
        """Map every transcript line onto the stored voice turns and rename it accordingly."""
        turns = self._db.speaker_turns(mid)
        if not turns:
            return
        names = {s["cluster"]: s["name"] for s in self._db.meeting_speakers(mid)}
        segs = self._db.get_segments(mid)
        for ch in ("Me", "Them"):
            speakers.label_segments(segs, turns, ch)
        for s in segs:
            s["speaker"] = names.get(s.get("cluster"), s["channel"])
        self._db.replace_segments(mid, segs)

    def rename_speaker(self, mid: int, cluster: str, name: str):
        """Rename one voice in a meeting and remember that voice for future meetings."""
        name = name.strip()[:60]
        spk = next((s for s in self._db.meeting_speakers(mid) if s["cluster"] == cluster), None)
        if not name or not spk:
            return None
        old = spk["name"]
        self._db.rename_cluster(mid, cluster, name)
        if spk["embedding"] is not None and name not in ("Me", "Them"):
            self._learn_voice(name, spk["embedding"], is_self=(spk["source"] == "you"))
        m = self._db.get_meeting(mid)
        if old not in ("Me", "Them") and old in m["ai_notes"]:
            self._db.update_meeting(mid, ai_notes=re.sub(rf"\b{re.escape(old)}\b", name, m["ai_notes"]))
        return self.get_meeting(mid)

    def list_voices(self):
        counts = self._db.voice_meeting_counts()
        return sorted(({"name": n, "samples": v["samples"], "is_self": v["is_self"], "updated_at": v["updated_at"],
                        "meetings": counts.get(n, 0)} for n, v in self._db.voices().items()),
                      key=lambda v: (not v["is_self"], v["name"].lower()))

    def delete_voice(self, name: str):
        self._db.delete_voice(name)
        self._voice_cache = None
        return True

    def _run_notes(self, mid: int):
        """Generate notes and block until done (used in the post-meeting pipeline)."""
        if self.generate_notes(mid):
            while mid in self._busy_notes:
                time.sleep(0.5)

    # --- high-accuracy transcript pass ----------------------------------
    def refine_meeting(self, mid: int):
        """Re-run the accurate pass on a meeting whose audio was kept."""
        if not self._tracks_for(mid) or mid == self._meeting_id or mid in self._pipeline:
            return False
        threading.Thread(target=self._refine_worker, args=(mid,), daemon=True).start()
        return True

    # --- languages -----------------------------------------------------------
    def _meeting_language(self, mid: int) -> str:
        m = self._db.get_meeting(mid)
        return (m and m.get("language")) or self._settings["meeting_language"]

    def _plan(self, language: str, refine_model: str | None = None) -> dict:
        return languages.speech_plan(language, self._settings["whisper_model"],
                                     refine_model or self._settings["refine_model"])

    def _configure_live(self, language: str):
        if self._cloud_mode():
            plan = languages.cloud_plan(language)
            self._transcriber.language, self._transcriber.seed = plan["language"], plan["seed"]
            return
        self._transcriber.seed = ""
        plan = self._plan(language)
        if plan["live"] != self._transcriber.model_name:
            self._transcriber.model_name = plan["live"]
            self._transcriber.unload()     # reloads on the next chunk
        self._transcriber.language = plan["live_language"]

    def set_meeting_language(self, mid: int, language: str):
        """Change a meeting's language; during a call the live transcript switches from the next sentence on."""
        if language not in languages.MEETING_LANGUAGES:
            return False
        self._db.update_meeting(mid, language=language)
        if mid == self._meeting_id:
            self._configure_live(language)
        return True

    def _notes_language(self, mid: int) -> str:
        return languages.notes_instruction(self._settings["notes_language"], self._meeting_language(mid))

    def _refine_worker(self, mid: int):
        if self._cloud_mode():
            return self._cloud_refine(mid)
        # "Improve transcript" can be asked for even when the automatic pass is off
        plan = self._plan(self._meeting_language(mid),
                          "large-v3-turbo" if self._settings["refine_model"] == "off" else None)
        model = plan["refine"]
        with self._refine_lock:
            self._emit("refine_start", meeting_id=mid)
            try:
                segs = refine(model, self._tracks_for(mid), self._settings["vocabulary"],
                              on_progress=lambda s: self._emit("refine_progress", meeting_id=mid, text=s),
                              turns=self._db.speaker_turns(mid), language=plan["refine_language"])
                if segs:
                    self._db.replace_segments(mid, segs)
                    self._apply_speaker_labels(mid)   # keep names on the new, more accurate lines
                self._emit("refine_done", meeting_id=mid, count=len(segs))
                return bool(segs)
            except Exception as e:
                self._emit("refine_error", meeting_id=mid, error=str(e))
                return False

    def _cloud_refine(self, mid: int) -> bool:
        """"Improve transcript" in cloud mode: transcribe the whole kept recording again in one go,
        which gives Whisper full context instead of short live chunks."""
        tracks = self._tracks_for(mid)
        with self._refine_lock:
            self._emit("refine_start", meeting_id=mid)
            try:
                whole = [{"channel": ch, "t_start": 0.0, "t_end": len(_read_wav(p)) / 16000} for ch, p in tracks.items()]
                plan = languages.cloud_plan(self._meeting_language(mid))
                segs = cloud_fill(self._groq, tracks, whole, plan["language"], self._settings["vocabulary"],
                                  on_progress=lambda s: self._emit("refine_progress", meeting_id=mid, text=s),
                                  seed=plan["seed"], piece_s=cloud.MAX_UPLOAD_S)
                if segs:
                    segs.sort(key=lambda s: s["t_start"])
                    self._db.replace_segments(mid, segs)
                    self._db.clear_gaps(mid)
                    self._apply_speaker_labels(mid)
                self._emit("refine_done", meeting_id=mid, count=len(segs))
                return bool(segs)
            except Exception as e:
                self._emit("refine_error", meeting_id=mid, error=error_text(e))
                return False

    # --- AI notes & questions --------------------------------------------
    def _ollama_ready(self, mid: int, event: str) -> bool:
        if self._cloud_mode():   # offline is fine here: the request is queued (notes) or reported (Ask)
            if not self._keys.get():
                self._emit(event, meeting_id=mid, error="Add your Groq API key in Settings → Cloud AI to use AI.")
                return False
            return True
        st = summarize.ollama_status(self._settings["ollama_model"])
        if not st["running"]:
            self._emit(event, meeting_id=mid, error="Ollama isn't running. Start the Ollama app and try again.")
            return False
        if not st["model_ready"]:
            self._emit(event, meeting_id=mid,
                       error=f"Model '{self._settings['ollama_model']}' isn't downloaded. Run: ollama pull {self._settings['ollama_model']}")
            return False
        return True

    def generate_notes(self, mid: int):
        if mid in self._busy_notes or not self._ollama_ready(mid, "notes_error"):
            return False
        self._busy_notes.add(mid)
        threading.Thread(target=self._notes_worker, args=(mid,), daemon=True).start()
        return True

    def _notes_worker(self, mid):
        try:
            self._write_notes(mid)
            if (self._db.get_meeting(mid)["cloud_pending"] and mid != self._meeting_id and mid not in self._pipeline
                    and not self._db.gaps(mid)):
                self._cloud_done(mid)    # a queued meeting finished by hand
        except CLOUD_WAIT_ERRORS as e:
            # Keep the request: the catch-up worker writes the notes once the cloud is reachable.
            self._db.set_cloud_pending(mid, NOTES_REQUESTED)
            self._cloud_issue = cloud_reason(e)
            self._emit("notes_queued", meeting_id=mid, reason=self._cloud_issue)
        except Exception as e:
            self._emit("notes_error", meeting_id=mid, error=str(e))
        finally:
            self._busy_notes.discard(mid)

    def _write_notes(self, mid):
        m = self._db.get_meeting(mid)
        self._emit("notes_start", meeting_id=mid)
        tpl = self._db.template(m.get("template_id")) or self._db.template("general")
        notes = summarize.generate_notes(
            self._notes_model(), m["title"], m["user_notes"], self._db.transcript_text(mid),
            me_name=self._settings["your_name"], structure=tpl["prompt"] if tpl else "",
            language=self._notes_language(mid),
            on_token=lambda t: self._emit("notes_token", meeting_id=mid, text=t),
            on_progress=lambda s: self._emit("notes_progress", meeting_id=mid, text=s),
        )
        notes = summarize.clean_notes(notes)
        self._db.update_meeting(mid, ai_notes=notes)
        self._emit("notes_done", meeting_id=mid, notes=notes)

    # --- templates --------------------------------------------------------
    def list_templates(self):
        return self._db.templates()

    def set_meeting_template(self, mid: int, tid: str):
        if not self._db.template(tid):
            return False
        self._db.update_meeting(mid, template_id=tid)
        return True

    def save_template(self, tpl: dict):
        name, prompt = (tpl.get("name") or "").strip(), (tpl.get("prompt") or "").strip()
        if not name or not prompt:
            return None
        return self._db.save_template(tpl.get("id"), name[:60], (tpl.get("description") or "").strip()[:140],
                                      prompt[:4000], tpl.get("icon") or "file")

    def delete_template(self, tid: str):
        self._db.delete_template(tid)
        if self._settings["default_template"] == tid:
            self.save_settings({"default_template": "auto"})
        return True

    # --- follow-up email ------------------------------------------------------
    def draft_followup(self, mid: int, tone: str = ""):
        if mid in self._busy_followup or not self._ollama_ready(mid, "followup_error"):
            return False
        tone = tone or self._settings["followup_tone"]
        if tone != self._settings["followup_tone"]:
            self.save_settings({"followup_tone": tone})
        self._busy_followup.add(mid)

        def work():
            try:
                m = self._db.get_meeting(mid)
                self._emit("followup_start", meeting_id=mid)
                text = summarize.followup_email(
                    self._notes_model(), m["title"], m["ai_notes"] or m["user_notes"],
                    self._db.transcript_text(mid), self._settings["your_name"], tone,
                    language=self._notes_language(mid),
                    on_token=lambda t: self._emit("followup_token", meeting_id=mid, text=t))
                self._db.update_meeting(mid, followup=text.strip())
                self._emit("followup_done", meeting_id=mid, text=text.strip())
            except Exception as e:
                self._emit("followup_error", meeting_id=mid, error=error_text(e))
            finally:
                self._busy_followup.discard(mid)

        threading.Thread(target=work, daemon=True).start()
        return True

    def save_followup(self, mid: int, text: str):
        self._db.update_meeting(mid, followup=text)
        return True

    def open_email(self, subject: str, body: str):
        """Hand the draft to the default mail app (Outlook, Mail, …)."""
        from urllib.parse import quote
        os.startfile(f"mailto:?subject={quote(subject)}&body={quote(body)}")
        return True

    # --- transcript editing ---------------------------------------------------
    def update_segment(self, seg_id: int, text: str):
        """Save an edited line. Returns words that are new to the line, as vocabulary suggestions."""
        text = " ".join(text.split())
        old = self._db.get_segment(seg_id)
        if not old or old["meeting_id"] in self._pipeline:
            return None    # processing would overwrite the edit; the UI only allows editing once it's done
        if not text:
            return self.delete_segment(seg_id) and {"deleted": True}
        self._db.update_segment(seg_id, text)
        old_words = {w.lower() for w in re.findall(r"[\w'-]+", old["text"])}
        vocab = {w.strip().lower() for w in self._settings["vocabulary"].replace("\n", ",").split(",")}
        new = [w for w in dict.fromkeys(re.findall(r"[\w'-]+", text))
               if w.lower() not in old_words and w.lower() not in vocab and len(w) > 2 and not w.isdigit()]
        return {"text": text, "suggest": new[:3]}

    def delete_segment(self, seg_id: int):
        return self._db.delete_segment(seg_id) is not None

    def add_vocabulary(self, words: list):
        current = [w.strip() for w in self._settings["vocabulary"].replace("\n", ",").split(",") if w.strip()]
        lower = {w.lower() for w in current}
        current += [w for w in words if w.lower() not in lower]
        self.save_settings({"vocabulary": ", ".join(current)})
        return self._settings["vocabulary"]

    def ask(self, mid: int, question: str, history: list):
        if not question.strip() or not self._ollama_ready(mid, "ask_error"):
            return False

        def work():
            try:
                m = self._db.get_meeting(mid)
                self._emit("ask_start", meeting_id=mid)
                answer = summarize.ask(
                    self._notes_model(), m["title"], m["user_notes"], m["ai_notes"],
                    self._db.transcript_text(mid), history or [], question,
                    on_token=lambda t: self._emit("ask_token", meeting_id=mid, text=t),
                )
                self._emit("ask_done", meeting_id=mid, text=answer)
            except Exception as e:
                self._emit("ask_error", meeting_id=mid, error=error_text(e))

        threading.Thread(target=work, daemon=True).start()
        return True

    # --- windows, widget & detection --------------------------------------
    def show_main(self, meeting_id: int | None = None):
        if self._main:
            try:
                self._main.show()
                self._main.restore()
                self._main.on_top = True
                self._main.on_top = False
            except Exception:
                pass
            if meeting_id:
                self._emit("open_meeting", meeting_id=meeting_id)
        return True

    def _main_in_front(self) -> bool:
        fg = _user32.GetForegroundWindow()
        if not fg:
            return False
        pid = ctypes.c_ulong()
        _user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
        if pid.value != os.getpid():
            return False
        buf = ctypes.create_unicode_buffer(64)
        _user32.GetWindowTextW(fg, buf, 64)
        return buf.value == "LocalNotes"

    def _pill_watch(self):
        """Keep the widget out of the way while LocalNotes itself is the active window."""
        while not self._quitting:
            time.sleep(0.3)
            self._sync_pill()

    def pill_state(self):
        if self._recorder:
            return {"mode": "recording", "meeting_id": self._meeting_id,
                    "elapsed": self._elapsed(), "paused": bool(self._paused_at),
                    "levels": self._recorder.levels(), "title": self._rec_title}
        if self._detected:
            return {"mode": "detected", "app": self._detected[0], "title": self._detected[1]}
        return {"mode": "idle"}

    def start_from_detection(self):
        if not self._detected:
            return None
        app, hint = self._detected
        mid = self.start_recording(hint, app)
        if mid:
            self.show_main(mid)
        return mid

    def dismiss_detection(self):
        self._detected = None
        self._emit("meeting_dismissed")
        self._sync_pill()
        return True

    def toggle_recording(self):
        if self._recorder:
            self.stop_recording()
        else:
            mid = self.start_recording()
            if mid:
                self.show_main(mid)

    def _sync_pill(self):
        if not self._pill:
            return
        want = (self._settings["show_widget"] and (self._recorder is not None or self._detected is not None)
                and not self._main_in_front())
        with self._pill_lock:
            if want == self._pill_visible:
                return
            self._pill_visible = want
        try:
            if want:
                # Showing a window activates it; hand focus straight back so the widget
                # never steals keystrokes from the meeting app or whatever the user is typing in.
                prev = _user32.GetForegroundWindow()
                self._pill.show()
                if prev:
                    _user32.SetForegroundWindow(prev)
            else:
                self._pill.hide()
        except Exception:
            pass

    def _on_meeting_start(self, app, hint):
        if self._recorder:
            return
        self._detected = (app, hint)
        if self._settings["auto_record"]:
            mid = self.start_recording(hint, app)
            if mid and self._tray:
                self._tray.notify("Recording started", f"{hint} — LocalNotes is taking notes.")
            return
        self._emit("meeting_detected", app=app, title=hint)
        if self._tray and not self._settings["show_widget"]:
            self._tray.notify(f"{hint} detected", "Open LocalNotes to start taking notes.")
        self._sync_pill()

    def _on_meeting_end(self, app):
        if self._detected and self._detected[0] == app:
            self._detected = None
        self._emit("meeting_ended", app=app)
        if self._recorder and self._meeting_app == app and self._settings["auto_stop"]:
            self.stop_recording()
        self._sync_pill()

    def hide_to_tray(self):
        if self._main:
            self._main.hide()
            if self._tray and not self._tray_hint_shown:
                self._tray_hint_shown = True
                self._tray.notify("LocalNotes is still running",
                                  "It will keep listening for meetings. Right-click the tray icon to quit.")

    def quit(self):
        self._quitting = True
        if self._recorder:
            rec, mid = self._recorder, self._meeting_id
            self._recorder = None
            rec.stop()
            if mid:
                self._db.end_meeting(mid)
        self._hotkeys.stop()
        if self._tray:
            self._tray.stop()
        for w in (self._pill, self._main):
            try:
                w.destroy()
            except Exception:
                pass


def _tasks(md: str) -> list[tuple[bool, str]]:
    out = []
    for line in md.splitlines():
        m = TASK_RE.match(line)
        if m:
            out.append((m.group(2).lower() == "x", m.group(4).strip()))
    return out


def _apply_icon(window):
    """WinForms windows default to Python's icon; use ours in the taskbar, Alt+Tab and title bar."""
    try:
        from System import Action
        from System.Drawing import Icon
        form = window.native
        form.Invoke(Action(lambda: setattr(form, "Icon", Icon(str(ICON_PATH)))))
    except Exception:
        pass


def main():
    background = "--background" in sys.argv
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        pass
    try:
        api = Api()
    except DataLockedError as e:
        _user32.MessageBoxW(None, str(e), "LocalNotes", 0x10)   # MB_ICONERROR
        return
    dark = api._settings["theme"] == "dark" or (api._settings["theme"] == "system" and system_prefers_dark())
    main_win = webview.create_window(
        "LocalNotes", url=str(UI_DIR / "index.html"), js_api=api,
        width=1320, height=860, min_size=(940, 620), hidden=background,
        background_color="#151412" if dark else "#F6F4EF",
    )
    try:
        screen_w = webview.screens[0].width
    except Exception:
        screen_w = 1920
    pill_win = webview.create_window(
        "LocalNotes widget", url=str(UI_DIR / "pill.html"), js_api=api,
        width=470, height=72, x=max(0, screen_w - 494), y=20,
        frameless=True, on_top=True, resizable=False, hidden=True, easy_drag=True,
        background_color="#161513",
    )
    api._main, api._pill = main_win, pill_win
    api._tray = Tray(
        on_open=lambda: api.show_main(),
        on_toggle_record=api.toggle_recording,
        is_recording=lambda: api._recorder is not None,
        is_detecting=lambda: api._settings["detect_meetings"],
        on_toggle_detect=lambda: api.save_settings({"detect_meetings": not api._settings["detect_meetings"]}),
        on_quit=api.quit,
    )

    def on_main_closing():
        if api._quitting:
            return True
        if api._settings["close_to_tray"]:
            threading.Thread(target=api.hide_to_tray, daemon=True).start()
        else:
            threading.Thread(target=api.quit, daemon=True).start()
        return False

    main_win.events.closing += on_main_closing
    main_win.events.shown += lambda: _apply_icon(main_win)
    pill_win.events.shown += lambda: _apply_icon(pill_win)
    pill_win.events.closing += lambda: api._quitting

    def on_start():
        api._tray.start()
        api.start_hotkeys()
        threading.Thread(target=api._pill_watch, daemon=True).start()

    webview.start(on_start, debug=bool(os.environ.get("LOCALNOTES_DEBUG")))


if __name__ == "__main__":
    main()
