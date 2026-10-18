# Architecture

LocalNotes is a single Python process. The UI is plain HTML/CSS/JS rendered by
[pywebview](https://pywebview.flowrl.com/) (Edge WebView2); everything else is Python.

## Modules

| File | Responsibility |
|---|---|
| `app.py` | Entry point. `Api` is the bridge the UI calls (`window.pywebview.api.*`); it owns recording, the post-call pipeline, settings, windows, tray and hotkeys. Python pushes live updates to the UI with `window.onEvent({type, ...})`. |
| `audio.py` | WASAPI capture of the microphone (“Me”) and system loopback (“Them”), resampled to 16 kHz with soxr, cut into utterance-sized chunks at pauses, and optionally written to per-speaker WAVs on a wall-clock timeline. Pause/resume. |
| `transcribe.py` | Live pass (`Transcriber`, a queue-fed worker using faster-whisper) and the batched after-call `refine()` pass. Echo suppression, hallucination filtering, custom-vocabulary prompting. |
| `speakers.py` | Speaker diarization and voice fingerprints with sherpa-onnx (pyannote segmentation + TitaNet embeddings); matching against saved voice profiles. |
| `summarize.py` | Prompts and streaming calls to Ollama: notes (with templates), Ask, follow-up emails, and output clean-up. |
| `templates.py` | Built-in note templates and the title-based template guess. |
| `languages.py` | Meeting languages (auto / English / Hindi / Hinglish): picks the live and after-call speech models and the Whisper language hint per meeting, and words the "write notes in" instruction. |
| `tools/build_hinglish_model.ps1` | Converts Oriserve's Hinglish Whisper models to CTranslate2 in a throwaway PyTorch environment (run by `setup.ps1`). |
| `db.py` | SQLCipher database: meetings, transcript segments, speakers, voice profiles, templates, FTS5 search. Migrates an unencrypted database in place, verified by row counts. |
| `securestore.py` | Database key generation and protection with Windows DPAPI. |
| `detector.py` | Call detection: apps currently holding the microphone (Windows consent store) plus window titles. |
| `hotkeys.py` | System-wide hotkeys via `RegisterHotKey` on a dedicated message-loop thread. |
| `tray.py` | System-tray icon and menu (pystray). |
| `selfcheck.py` | Install verification used by `setup.ps1`. |
| `logo.py` | The logo, defined once and rendered to `assets/` (SVG, PNG, multi-size ICO), `ui/logo.svg`, and the tray icon at runtime. Run `python logo.py` after changing it. |
| `ui/` | `index.html` + `app.js` + `style.css` for the main window, with `glass.css` layering the default Glass style on top (scoped to `html[data-style="glass"]`; without it you get Classic); `pill.html` for the floating widget. |

## Recording and the post-call pipeline

```
start_recording ──► Recorder (mic + loopback) ──► chunks ──► Transcriber ──► live segments (DB + UI)
                                    │
                                    └──► Me.wav / Them.wav  (16 kHz, wall-clock aligned)

stop_recording ──► _finish():
   1. transcribing   wait for the live queue to drain
   2. speakers       diarize each track, name clusters (you / saved profile / Speaker N), label segments
   3. notes          quick notes from the live transcript (template-aware)
   4. refining       batched large-v3-turbo pass replaces the transcript; speaker labels re-applied
   5. renotes        notes rewritten from the accurate transcript
   6. done           audio deleted unless "Keep call audio" is on
```

Each stage is emitted as a `pipeline` event so the UI can show progress. Transcript editing is disabled while a
meeting is in the pipeline, because the refine pass replaces every segment.

## Data

Everything lives in `%APPDATA%\LocalNotes` (override with `LOCALNOTES_DATA`):

| Path | Contents |
|---|---|
| `localnotes.db` | SQLCipher database (AES-256) |
| `storage.key` | 32-byte database key, encrypted with DPAPI |
| `settings.json` | Preferences (not sensitive) |
| `models/` | Speaker-recognition models |
| `audio/<meeting id>/` | Per-speaker WAVs while a call is processed (or kept, if enabled) |

Whisper models are cached by Hugging Face Hub in `%USERPROFILE%\.cache\huggingface`; the Ollama model is managed by Ollama.

## Speaker recognition

After a call, each track is diarized into voice clusters. Each cluster gets a fingerprint (the duration-weighted
average of TitaNet embeddings of its longest turns). The dominant voice on the microphone is the user; other
clusters are matched one-to-one against saved profiles by cosine similarity (≥ 0.60 and a 0.05 margin over the
runner-up). Unmatched clusters become “Speaker N”. Renaming a speaker stores or updates that person's profile
(a capped running average), and confident matches keep adapting it. During the call, “Them” chunks are matched
against profiles with a stricter threshold so known voices appear named live.
