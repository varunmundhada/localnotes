# Security and privacy

## What stays on your PC

LocalNotes does not have a server. Recordings, transcripts, notes, voice profiles and settings are stored only on
your machine.

### Cloud engine (default)

To transcribe and write notes, LocalNotes sends data to [Groq](https://groq.com) over HTTPS
(`api.groq.com`), using your own API key:

- audio chunks of the call (live, and any stretches missed while offline), with the meeting's custom vocabulary;
- the transcript, your rough notes and the meeting title, to write notes, follow-up emails and answers in Ask.

Speaker recognition and voice profiles never leave the PC. Groq states that it does not use API inputs or outputs
to train models; see its [privacy policy](https://groq.com/privacy-policy/) for retention. The API key is stored in
`%APPDATA%\LocalNotes\groq.key`, encrypted with Windows DPAPI; LocalNotes never shows it again after saving.

### On-device engine

With *Settings → Cloud AI → This PC only*, nothing is sent anywhere. Network access is only used to download
models (during setup, or the first time a model is used) from:

- Hugging Face Hub — Whisper speech models
- GitHub releases of `k2-fsa/sherpa-onnx` — speaker-recognition models (used by both engines)
- Ollama's registry — the local language model

The local language model is served by Ollama on `127.0.0.1:11434` and is not reachable from other machines by default.

## Data at rest

- The database (`%APPDATA%\LocalNotes\localnotes.db`) is encrypted with SQLCipher, AES-256.
- Its 256-bit key is random, generated on first run, and stored in `storage.key` encrypted with Windows DPAPI
  (`CryptProtectData`), which binds it to your Windows user account. Copying the data folder to another account
  or PC leaves it unreadable.
- Call audio is written as plain WAV files while a call is being processed and deleted afterwards (in cloud mode,
  once everything has been transcribed, which can be later if you were offline). If you enable *Keep call audio*,
  those files remain unencrypted in `%APPDATA%\LocalNotes\audio`.
- Anyone who can sign in to your Windows account can use the app and therefore read your notes. Use a strong
  Windows password and disk encryption (BitLocker) for full protection.

## Recording consent

Laws on recording conversations differ between countries and states; some require everyone's consent. LocalNotes
does not notify other participants. You are responsible for telling people they are being recorded.

## Reporting a vulnerability

Please report security issues privately to the maintainer via GitHub (open a private security advisory on the
repository) rather than in a public issue.
