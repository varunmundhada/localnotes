# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- New app logo and icons: sidebar, window and taskbar, tray (turns red while recording), shortcuts and README.
  Generated from `logo.py`, so every size and format comes from the same shapes.
- LocalNotes now has its own taskbar identity instead of grouping under Python.
- Glass style (new default): floating frosted panels over a soft aurora, glossy controls, light edges and a sheen
  that follows the cursor; dark-glass recording bar, menus and dialogs. The previous look stays available as
  “Classic” in Settings → App → Style.

- Regression test suite (`tests/`), run by CI on every push.

### Fixed
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
