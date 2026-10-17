"""System-tray icon so LocalNotes keeps detecting meetings while its window is closed."""
import pystray
from PIL import Image, ImageDraw

ACCENT = (31, 122, 92)
LIVE = (229, 72, 77)


def _icon(color) -> Image.Image:
    """Rounded square with a small waveform, drawn at 4x and downsampled for smooth edges."""
    s = 256
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((8, 8, s - 8, s - 8), radius=64, fill=color)
    bars = [0.30, 0.62, 0.92, 0.55, 0.78, 0.38]
    w, gap = 22, 14
    x = (s - (len(bars) * w + (len(bars) - 1) * gap)) // 2
    for h in bars:
        bh = int(h * 140)
        d.rounded_rectangle((x, s // 2 - bh // 2, x + w, s // 2 + bh // 2), radius=11, fill=(255, 255, 255))
        x += w + gap
    return img.resize((64, 64), Image.LANCZOS)


class Tray:
    def __init__(self, on_open, on_toggle_record, is_recording, is_detecting, on_toggle_detect, on_quit):
        self._is_recording = is_recording
        self._idle_img, self._live_img = _icon(ACCENT), _icon(LIVE)
        menu = pystray.Menu(
            pystray.MenuItem("Open LocalNotes", lambda: on_open(), default=True),
            pystray.MenuItem(lambda _: "Stop recording" if is_recording() else "Start recording",
                             lambda: on_toggle_record()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Detect meetings", lambda: on_toggle_detect(),
                             checked=lambda _: is_detecting()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda: on_quit()),
        )
        self._icon = pystray.Icon("LocalNotes", self._idle_img, "LocalNotes", menu)

    def start(self):
        self._icon.run_detached()

    def refresh(self):
        live = self._is_recording()
        self._icon.icon = self._live_img if live else self._idle_img
        self._icon.title = "LocalNotes — recording" if live else "LocalNotes"
        self._icon.update_menu()

    def notify(self, title: str, message: str):
        try:
            self._icon.notify(message, title)
        except Exception:
            pass

    def stop(self):
        try:
            self._icon.stop()
        except Exception:
            pass
