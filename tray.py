"""System-tray icon so LocalNotes keeps detecting meetings while its window is closed."""
import pystray

import logo


class Tray:
    def __init__(self, on_open, on_toggle_record, is_recording, is_detecting, on_toggle_detect, on_quit):
        self._is_recording = is_recording
        self._idle_img, self._live_img = logo.render(64), logo.render(64, "live")   # red tile while recording
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
