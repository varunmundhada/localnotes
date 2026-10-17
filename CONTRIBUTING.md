# Contributing

## Setup

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -NoShortcuts
```

## Workflow

1. Create a branch: `git checkout -b feature/short-name`.
2. Work against a throwaway data folder so your real meetings are never touched:
   ```powershell
   $env:LOCALNOTES_DATA = "$env:TEMP\ln-dev"
   $env:LOCALNOTES_DEBUG = "1"     # WebView developer tools
   .venv\Scripts\python.exe app.py
   ```
3. Before committing, run the checks (CI runs the first three):
   ```powershell
   .venv\Scripts\python.exe -m compileall -q .
   node --check ui\app.js
   .venv\Scripts\python.exe -m unittest discover -s tests -v
   .venv\Scripts\python.exe selfcheck.py
   ```
   The tests need no audio devices, models or Ollama, and use a throwaway data folder.
   When you fix a bug, add a test that fails without the fix.
4. Add a line under **Unreleased** in [CHANGELOG.md](CHANGELOG.md).
5. Open a pull request.

## Conventions

- **Privacy first.** No feature may send meeting content off the machine. Downloads of models are the only network use.
- **Database changes** go in `db.py` as additive migrations (`ALTER TABLE ... ADD COLUMN` guarded by `PRAGMA table_info`), never destructive ones — users' existing databases must keep working.
- **New settings** need a default in `DEFAULT_SETTINGS` (`app.py`) and a control in the Settings dialog.
- **UI ↔ Python:** UI calls `api.<method>()`; long work runs on a thread and reports back with `self._emit(...)`, handled in `window.onEvent` in `ui/app.js`.
- **Pinned dependencies** in `requirements.txt`; bump deliberately and re-run `selfcheck.py`.
- Keep comments sparse and about *why*, matching the existing code.

## Releasing

1. Move the **Unreleased** notes in `CHANGELOG.md` under a new version heading.
2. Update `__version__` in `app.py`.
3. Commit, then tag: `git tag v0.2.0 && git push --tags`.
