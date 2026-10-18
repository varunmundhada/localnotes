# Architecture

LocalNotes is a single Python process. The UI is plain HTML/CSS/JS rendered by
[pywebview](https://pywebview.flowrl.com/) (Edge WebView2); everything else is Python.

## Modules

| File | Responsibility |
|---|---|
| `app.py` | Entry point. `Api` is the bridge the UI calls (`window.pywebview.api.*`); it owns recording, the post-call pipeline, settings, windows, tray and hotkeys. Python pushes live updates to the UI with `window.onEvent({type, ...})`. |
| `audio.py` | WASAPI capture of the microphone (“Me”) and system loopback (“Them”), resampled to 16 kHz with soxr, cut into utterance-sized chunks at pauses (5–20 s on-device, 10–30 s for the cloud), and optionally written to per-speaker WAVs on a wall-clock timeline. Pause/resume. |
| `cloud.py` | Groq client (OpenAI-compatible API): Whisper large-v3 transcription and streamed chat completions. Sorts failures into what the caller should do — `Offline` (queue and retry later), `RateLimited` (wait), `AuthError` (no/bad key: keep queued), `CloudError` (report). `KeyStore` keeps the API key DPAPI-encrypted. |
| `transcribe.py` | Live pass (`Transcriber`, a queue-fed worker: Groq in cloud mode, faster-whisper on-device), `cloud_fill()` for stretches the cloud missed, and the batched on-device `refine()` pass. Echo suppression, hallucination filtering, custom-vocabulary prompting. |
| `speakers.py` | Speaker diarization and voice fingerprints with sherpa-onnx (pyannote segmentation + TitaNet embeddings); matching against saved voice profiles. |
| `summarize.py` | Prompts and streaming calls for notes (with templates), Ask and follow-up emails, and output clean-up. Cloud models (`cloud.CHAT_MODELS`) go to Groq, anything else to Ollama. Cloud requests are sized by estimated tokens to fit the free tier's per-minute limits. |
| `templates.py` | Built-in note templates and the title-based template guess. |
| `languages.py` | Meeting languages (auto / English / Hindi / Hinglish): picks the speech models and the Whisper language hint per meeting (`speech_plan` on-device, `cloud_plan` for Groq), and words the "write notes in" instruction. |
| `tools/build_hinglish_model.ps1` | Converts Oriserve's Hinglish Whisper models to CTranslate2 in a throwaway PyTorch environment (run by `setup.ps1`). |
| `db.py` | SQLCipher database: meetings, transcript segments, speakers, voice profiles, templates, FTS5 search. Migrates an unencrypted database in place, verified by row counts. |
| `securestore.py` | Database key generation and protection with Windows DPAPI. |
| `detector.py` | Call detection: apps currently holding the microphone (Windows consent store) plus window titles. |
| `hotkeys.py` | System-wide hotkeys via `RegisterHotKey` on a dedicated message-loop thread. |
| `tray.py` | System-tray icon and menu (pystray). |
| `selfcheck.py` | Install verification used by `setup.ps1`. |
| `logo.py` | The logo, defined once and rendered to `assets/` (SVG, PNG, multi-size ICO), `ui/logo.svg`, and the tray icon at runtime. Run `python logo.py` after changing it. |
| `ui/` | `index.html` + `app.js` + `style.css` for the main window, with `glass.css` layering the default Glass style on top (scoped to `html[data-style="glass"]`; without it you get Classic); `pill.html` for the floating widget. |

## Engines

`settings.engine` picks where the AI runs. Recording, storage and speaker recognition are the same for both.

| | `cloud` (default) | `local` |
|---|---|---|
| Live transcript | Groq Whisper large-v3, 10–30 s chunks | faster-whisper base.en (multilingual / Hinglish Swift for other languages) |
| After the call | missed stretches only (`cloud_fill`) | accuracy pass, large-v3-turbo or Hinglish Apex |
| Notes, Ask, follow-ups | Groq: `cloud_model` (Qwen 3.8 27B) | Ollama: `ollama_model` |
| Network | `api.groq.com` only | none |

## Recording and the post-call pipeline (on-device)

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

## Cloud mode and the offline queue

```
start_recording ──► meetings.cloud_pending = 1, audio always saved
live chunk ──► Groq ──► segment
          └─ offline / rate limit / no key ──► pending_audio(meeting, channel, t_start, t_end)
                                               (later chunks skip the network for 20 s)
stop_recording ──► _finish(): transcribing ─► speakers ─► _cloud_complete():
                       catchup   transcribe pending_audio from the WAVs (merged into ≤ 60 s requests)
                       notes     written by the cloud model
                       done      cloud_pending = 0, audio deleted
                    any step offline ──► stage "waiting": the meeting stays usable, audio is kept
```

`_catchup_loop` retries queued meetings: 5 s after start (so meetings interrupted by a quit or crash are finished),
every 30 s with back-off to 10 minutes while offline, and immediately when the live transcriber reconnects or the
user tests the connection. It does nothing in on-device mode. Notes asked for while offline set
`cloud_pending = 2`, so they are written even with automatic notes turned off.

Groq's free tier allows, per model and minute, about 8K tokens in total, 7K input tokens and, for Qwen 3.8, 1K output
tokens. `summarize` keeps each prompt under ~4.5K estimated tokens, caps the answer per model
(`cloud.OUTPUT_LIMITS`), condenses long transcripts in parts alternating between two models (each has its own
allowance), and waits out 429s using the wait time Groq reports. A request too large for a whole minute's allowance is
an error, not a wait.

## Data

Everything lives in `%APPDATA%\LocalNotes` (override with `LOCALNOTES_DATA`):

| Path | Contents |
|---|---|
| `localnotes.db` | SQLCipher database (AES-256) |
| `storage.key` | 32-byte database key, encrypted with DPAPI |
| `groq.key` | Groq API key, encrypted with DPAPI (a plain `groq-key.txt` dropped here is imported and deleted on start) |
| `settings.json` | Preferences (not sensitive) |
| `models/` | Speaker-recognition models |
| `audio/<meeting id>/` | Per-speaker WAVs while a call is processed or waiting for the cloud (or kept, if enabled) |

On-device only: Whisper models are cached by Hugging Face Hub in `%USERPROFILE%\.cache\huggingface`, the Hinglish
models live in `models/`, and the Ollama model is managed by Ollama.

## Speaker recognition

After a call, each track is diarized into voice clusters. Each cluster gets a fingerprint (the duration-weighted
average of TitaNet embeddings of its longest turns). The dominant voice on the microphone is the user; other
clusters are matched one-to-one against saved profiles by cosine similarity (≥ 0.60 and a 0.05 margin over the
runner-up). Unmatched clusters become “Speaker N”. Renaming a speaker stores or updates that person's profile
(a capped running average), and confident matches keep adapting it. During the call, “Them” chunks are matched
against profiles with a stricter threshold so known voices appear named live.
