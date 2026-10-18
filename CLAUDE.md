# LocalNotes — notes for Claude Code

Private AI meeting notes for Windows: Python 3.12 + pywebview UI, Groq cloud (Whisper large-v3 + chat models) by
default or faster-whisper + Ollama on-device, sherpa-onnx, SQLCipher. Read `README.md` for the product and `docs/ARCHITECTURE.md` for the code layout.

## Installing on a new PC

When asked to install or set up this project:

1. Clone it (if not already cloned) and run from the repo root:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\setup.ps1
   ```
   It installs Python 3.12 with winget if missing, creates `.venv`, installs `requirements.txt`, downloads the
   speaker models (45 MB), creates Start Menu and desktop shortcuts, and ends with `selfcheck.py`. Add `-Local` only
   if the user wants on-device AI (Ollama + Whisper + Hinglish models, ~5 GB, slow — run it in the background).
   It is idempotent: re-run it after fixing any failure.
2. Success = the self-check prints `LocalNotes is ready.` (exit code 0). `WARN` lines are non-essential
   (e.g. Ollama not running yet); report them to the user with the fix the line suggests.
3. If `py -3.12` was just installed, a new terminal may be needed for PATH; the script also checks the default
   install location.
4. Tell the user: get a free key at https://console.groq.com/keys and paste it in LocalNotes → Settings → Cloud AI;
   launch from the Start Menu ("LocalNotes"); `Ctrl+Alt+N` starts recording from anywhere.
   Never ask the user to paste the key into chat. If they want to hand it over without the UI, they can save it as
   `%APPDATA%\LocalNotes\groq-key.txt`; the app imports it (DPAPI-encrypted) and deletes the plain file on start.

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
  `.venv\Scripts\python.exe -m unittest discover -s tests -v`, `.venv\Scripts\python.exe selfcheck.py`.
  Add a regression test in `tests/` for every bug fix (it should fail without the fix).
- Do not drive the UI with simulated keystrokes (SendKeys): focus can move to another window and type there.
  Use mouse clicks for visual checks, or call `Api` methods directly from Python.
- Long-running Python work runs on threads and reports to the UI via `self._emit(kind, ...)`, handled in
  `window.onEvent` in `ui/app.js`.
- Database changes: additive migrations only, in `DB.__init__`.
- Add user-visible changes under **Unreleased** in `CHANGELOG.md`; bump `__version__` in `app.py` on release.
- Meeting content is stored only on the machine. The only network calls allowed are to Groq (`cloud.py`, cloud
  engine) and model downloads; no telemetry. On-device mode must make no cloud calls at all.
- Never print, log or return the Groq key (`cloud.KeyStore`); use `hint()` (last 4 characters) when showing it.
- Tests must never call the real Groq API: use the `FakeGroq` stand-in in `tests/test_cloud.py`.
