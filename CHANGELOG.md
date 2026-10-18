# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **Cloud AI with Groq (new default).** Speech-to-text (Whisper large-v3) and notes, Ask and follow-up emails
  (Qwen 3.8 27B, or GPT-OSS 120B / 20B) run on Groq's free tier, so the laptop no longer runs heavy models.
  Recordings, notes and voice profiles still stay on the PC; speaker recognition still runs locally. Add your free
  API key in **Settings → Cloud AI**; it is checked straight away and stored encrypted with your Windows account.
  “This PC only” keeps the previous fully on-device engine (`setup.ps1 -Local` installs its models).
- **Works without internet.** Recording carries on when the connection drops; the audio is kept, and the missed
  transcript and the notes are finished automatically when you're back online — also after a restart. Queued
  meetings are marked “Waiting to finish”, with a **Try now** button.
- Hinglish in the cloud uses Whisper's Hindi mode primed with code-switched text, which matched the on-device
  Hinglish model's accuracy in testing (Hindi words are written in Devanagari).
- Long meetings are condensed in parts sized to Groq's free-tier limits, with automatic waits when a limit is hit.
- **Hindi and Hinglish meetings.** A meeting language (Auto / English / Hindi / Hinglish) — a default in
  Settings and a language chip on every meeting, switchable mid-call. Hinglish uses Oriserve's
  Whisper-Hindi2Hinglish models (Swift live, Apex after the call), converted locally by
  `tools/build_hinglish_model.ps1`; Hindi uses multilingual Whisper with an explicit language hint instead of
  unreliable auto-detection. A "Write notes in" setting produces English, Hindi or Hinglish notes from any meeting.
- Notes model is now Qwen3 4B Instruct, which understands and writes Hindi/Hinglish (Llama 3.2 3B couldn't).
  Existing installs on the old default are moved over automatically. Reasoning ("thinking") output from
  thinking models is kept out of the notes.
- New app logo and icons: sidebar, window and taskbar, tray (turns red while recording), shortcuts and README.
  Generated from `logo.py`, so every size and format comes from the same shapes.
- LocalNotes now has its own taskbar identity instead of grouping under Python.
- Glass style (new default): floating frosted panels over a soft aurora, glossy controls, light edges and a sheen
  that follows the cursor; dark-glass recording bar, menus and dialogs. The previous look stays available as
  “Classic” in Settings → App → Style.

- Regression test suite (`tests/`), run by CI on every push.
- One glass material system (thin / regular / thick) applied everywhere — dialogs, the command palette,
  menus, toasts, form fields, selects, switches, chips and keys are now frosted glass too, instead of
  near-solid white.
- Settings redesigned: a section rail (Meetings, Transcription, Speakers, AI & templates, Shortcuts,
  Appearance, Privacy) and instant save — every change applies immediately, with a “Saved” confirmation,
  so nothing is lost by closing the dialog. A shortcut reference lists all in-app keys.
- Deleting a meeting is instant with a 6-second Undo, instead of “click again to confirm”.
- Each meeting in the sidebar has a “…” menu (Open, Rename, Copy notes, Delete), also on right-click.
- Keyboard and screen-reader support: sidebar items and cards are focusable and open with Enter, menus
  support arrow keys, a consistent focus ring, and labels on icon-only buttons.
- Smaller touches: times read “2:07 am”, the people chip shows it's a menu, transcript lines show an
  edit hint on hover, and menus flip upward near the bottom of the window.

### Changed
- `setup.ps1` no longer downloads ~5 GB of local models by default; pass `-Local` for the on-device engine.

### Fixed
- The AI no longer invents action items when nobody committed to anything.
- Whisper's "nan" hallucination on near-silent audio is filtered out.
- After the accuracy pass, transcript lines could merge several people's speech into one ~30 s line, losing
  speaker names. The pass now transcribes each speaker turn separately, so every line belongs to one voice.
- Starting a new recording while the previous call was still being processed could silently drop the new
  recording's audio. Meetings also can't be deleted while they're being processed any more.
- A lost or unreadable encryption key made LocalNotes crash on start — and quietly created a new key that
  could never open the existing notes. It now explains what happened and leaves everything untouched.
- The same shortcut could be assigned to both start/stop and pause; one of them would silently stop working.
- A stray space before the summary line when the AI writes `**Summary:** …`.
- Speakers in the same meeting always get different colours.
- Segmented-control highlights now line up after fonts load.

### Planned
- Read participant names directly from Zoom / Teams / Meet windows to name first-time speakers.
- Calendar integration for meeting titles and attendees.
- Ask across all meetings.

## [0.1.0] — 2026-10-18

First release.

### Added
- Two-sided recording (microphone + system audio) with live Whisper transcription.
- After-call accuracy pass with Whisper large-v3-turbo (batched for CPU).
- Speaker separation and persistent voice profiles; rename a speaker once to have them recognised in future meetings.
- Local AI notes via Ollama, with eight built-in templates and custom templates.
- Ask questions about a meeting; follow-up email drafts with tone options.
- Automatic call detection for Zoom, Teams, Google Meet, Webex, Slack and Discord, with a floating widget.
- System tray, launch at sign-in, global hotkeys, pause / resume.
- Editable transcript with custom-vocabulary suggestions.
- SQLCipher (AES-256) database encryption with a DPAPI-protected key; automatic migration of unencrypted databases.
- Full-text search, action items across meetings, command palette, light and dark themes.
- `setup.ps1` installer and `selfcheck.py` install verification.
