"""Detect when a Zoom / Teams / Google Meet call starts or ends.

Windows records which apps currently hold the microphone under the
CapabilityAccessManager consent store (LastUsedTimeStop == 0 means "in use now").
That is combined with visible window titles to tell which meeting app it is.
"""
import ctypes
import os
import sys
import threading
import time
import winreg
from ctypes import wintypes

MIC_KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
BROWSERS = ("chrome", "msedge", "firefox", "brave", "opera", "arc", "vivaldi")
POLL_S = 3.0
CONFIRM_POLLS = 2       # require the same state this many polls in a row

_user32 = ctypes.windll.user32
_EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _mic_users() -> list[str]:
    """Lower-cased registry names of apps currently using the microphone."""
    users = []
    for sub in (MIC_KEY, MIC_KEY + r"\NonPackaged"):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub)
        except OSError:
            continue
        with key:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(key, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(key, name) as app:
                        stop, _ = winreg.QueryValueEx(app, "LastUsedTimeStop")
                        start, _ = winreg.QueryValueEx(app, "LastUsedTimeStart")
                    if stop == 0 and start > 0:
                        users.append(name.lower())
                except OSError:
                    pass
    return users


def _window_titles() -> list[str]:
    titles = []

    def cb(hwnd, _):
        if _user32.IsWindowVisible(hwnd):
            n = _user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                _user32.GetWindowTextW(hwnd, buf, n + 1)
                titles.append(buf.value)
        return True

    _user32.EnumWindows(_EnumProc(cb), 0)
    return titles


def detect_meeting() -> tuple[str, str] | None:
    """Return (app, title_hint) for an active call, or None."""
    me = os.path.basename(sys.executable).lower()
    users = [u for u in _mic_users() if me not in u]
    if not users:
        return None
    titles = _window_titles()
    if any("zoom" in u for u in users):
        hint = next((t for t in titles if "zoom meeting" in t.lower()), "Zoom meeting")
        return "zoom", hint
    if any("teams" in u for u in users):
        hint = next((t.split("|")[0].strip() for t in titles
                     if "microsoft teams" in t.lower() and "|" in t), "Teams meeting")
        return "teams", hint or "Teams meeting"
    if any(b in u for u in users for b in BROWSERS):
        meet = next((t for t in titles if t.startswith("Meet - ") or "meet.google.com" in t), None)
        if meet:
            name = meet.removeprefix("Meet - ").split(" - ")[0].strip()
            return "meet", f"Meet: {name}" if name else "Google Meet"
    if any(x in u for u in users for x in ("webex", "slack", "discord")):
        app = next(x for x in ("webex", "slack", "discord") if any(x in u for u in users))
        return app, f"{app.title()} call"
    return None


class MeetingDetector:
    """Polls in the background; calls on_start(app, hint) / on_end(app) on transitions."""

    def __init__(self, on_start, on_end):
        self.on_start, self.on_end = on_start, on_end
        self.enabled = True
        self.current: tuple[str, str] | None = None
        self._candidate, self._count = None, 0
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            time.sleep(POLL_S)
            if not self.enabled:
                continue
            try:
                found = detect_meeting()
            except Exception:
                continue
            app = found[0] if found else None
            cur = self.current[0] if self.current else None
            if app == cur:
                self._candidate, self._count = None, 0
                continue
            if app == self._candidate:
                self._count += 1
            else:
                self._candidate, self._count = app, 1
            if self._count >= CONFIRM_POLLS:
                prev = self.current
                self.current = found
                self._candidate, self._count = None, 0
                if prev:
                    self.on_end(prev[0])
                if found:
                    self.on_start(*found)
