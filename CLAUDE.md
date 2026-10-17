# LocalNotes — notes for Claude Code

Private, local AI meeting notes for Windows: Python 3.12 + pywebview UI, faster-whisper, sherpa-onnx, Ollama,
SQLCipher. Read `README.md` for the product and `docs/ARCHITECTURE.md` for the code layout.

## Installing on a new PC

When asked to install or set up this project:

1. Clone it (if not already cloned) and run from the repo root:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\setup.ps1
   ```
   It installs Python 3.12 and Ollama with winget if missing, creates `.venv`, installs `requirements.txt`,
   downloads models (~4 GB total, takes a while — run it in the background and wait), creates Start Menu and
   desktop shortcuts, and ends with `selfcheck.py`. It is idempotent: re-run it after fixing any failure.
2. Success = the self-check prints `LocalNotes is ready.` (exit code 0). `WARN` lines are non-essential
   (e.g. Ollama not running yet); report them to the user with the fix the line suggests.
3. If `py -3.12` was just installed, a new terminal may be needed for PATH; the script also checks the default
   install location.
4. Tell the user: launch from the Start Menu ("LocalNotes"), and that `Ctrl+Alt+N` starts recording from anywhere.

Windows only. Do not try to install on macOS/Linux.

## Updating an existing install

```powershell
git pull
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -NoShortcuts
```
Quit the running app first (tray icon → Quit). The database migrates itself on next launch.

## Working on the code

- Run from source: `.venv\Scripts\python.exe app.py`.
- **Never test against the user's real data.** Set `LOCALNOTES_DATA` to a temp folder before launching or importing
  `app`; real meetings live in `%APPDATA%\LocalNotes`.
- Checks before committing: `.venv\Scripts\python.exe -m compileall -q .`, `node --check ui\app.js`,
  `.venv\Scripts\python.exe selfcheck.py`.
- Do not drive the UI with simulated keystrokes (SendKeys): focus can move to another window and type there.
  Use mouse clicks for visual checks, or call `Api` methods directly from Python.
- Long-running Python work runs on threads and reports to the UI via `self._emit(kind, ...)`, handled in
  `window.onEvent` in `ui/app.js`.
- Database changes: additive migrations only, in `DB.__init__`.
- Add user-visible changes under **Unreleased** in `CHANGELOG.md`; bump `__version__` in `app.py` on release.
- Keep meeting content on the machine: no telemetry, no cloud calls.
