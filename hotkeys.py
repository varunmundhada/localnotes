"""System-wide hotkeys via RegisterHotKey — they work while LocalNotes is hidden in the tray."""
import ctypes
import threading
from ctypes import wintypes

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
MODS = {"alt": 0x1, "ctrl": 0x2, "shift": 0x4, "win": 0x8}
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
_NAMED_KEYS = {"space": 0x20, "enter": 0x0D, "tab": 0x09, "home": 0x24, "end": 0x23,
               "insert": 0x2D, "pause": 0x13, "`": 0xC0, ",": 0xBC, ".": 0xBE, "/": 0xBF, ";": 0xBA}


def parse(combo: str) -> tuple[int, int] | None:
    """'ctrl+alt+n' -> (modifiers, virtual key). Requires at least one modifier."""
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if len(parts) < 2:
        return None
    mods = 0
    for p in parts[:-1]:
        if p not in MODS:
            return None
        mods |= MODS[p]
    key = parts[-1]
    if len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x6F + int(key[1:])
    elif key in _NAMED_KEYS:
        vk = _NAMED_KEYS[key]
    else:
        return None
    return mods, vk


class Hotkeys:
    """bindings: {combo: callback}. Callbacks run on their own thread so they can't stall the listener."""

    def __init__(self):
        self._thread: threading.Thread | None = None
        self._tid = 0
        self.failed: list[str] = []

    def start(self, bindings: dict):
        self.stop()
        ready = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(bindings, ready), daemon=True)
        self._thread.start()
        ready.wait(2)

    def _run(self, bindings: dict, ready: threading.Event):
        self._tid = _kernel32.GetCurrentThreadId()
        actions, self.failed = {}, []
        for i, (combo, fn) in enumerate(bindings.items(), start=1):
            parsed = parse(combo) if combo else None
            if not parsed:
                continue
            if _user32.RegisterHotKey(None, i, parsed[0] | MOD_NOREPEAT, parsed[1]):
                actions[i] = fn
            else:
                self.failed.append(combo)    # usually: another app already owns this shortcut
        ready.set()
        msg = wintypes.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and msg.wParam in actions:
                threading.Thread(target=actions[msg.wParam], daemon=True).start()
        for i in actions:
            _user32.UnregisterHotKey(None, i)

    def stop(self):
        if self._thread and self._thread.is_alive():
            _user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
            self._thread.join(2)
        self._thread = None
