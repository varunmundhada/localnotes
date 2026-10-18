# Third-party notices

LocalNotes' own source code is MIT-licensed (see [LICENSE](LICENSE)). It does not bundle third-party code or
models in this repository: Python packages are installed from PyPI, and models are downloaded from their
publishers by `setup.ps1` or on first use. Their licences apply to those downloads.

## Models

| Model | Used for | Source | Licence |
|---|---|---|---|
| Whisper base.en, large-v3-turbo (CTranslate2 conversions) | Speech-to-text | Hugging Face (Systran, mobiuslabsgmbh) | MIT |
| pyannote segmentation-3.0 (ONNX export) | Speaker segmentation | k2-fsa/sherpa-onnx releases | MIT |
| NVIDIA NeMo TitaNet small (ONNX export) | Speaker embeddings | k2-fsa/sherpa-onnx releases | CC-BY-4.0 |
| Oriserve Whisper-Hindi2Hinglish Apex & Swift (converted locally to CTranslate2) | Hinglish speech-to-text | Hugging Face (Oriserve) | Apache-2.0 |
| Qwen3 4B Instruct | Notes, Ask, follow-up emails | Ollama registry | Apache-2.0 |
| Llama 3.2 3B (optional, previous default) | Notes, Ask, follow-up emails | Ollama registry | Llama 3.2 Community License |

## Python packages

| Package | Licence |
|---|---|
| faster-whisper | MIT |
| sherpa-onnx | Apache-2.0 |
| pywebview | BSD-3-Clause |
| PyAudioWPatch | MIT |
| sqlcipher3-wheels / SQLCipher Community Edition | zlib / BSD-style |
| soxr (python-soxr, libsoxr) | LGPL-2.1 |
| pystray | LGPL-3.0 |
| Pillow | MIT-CMU (HPND) |
| NumPy | BSD-3-Clause |
| requests | Apache-2.0 |

## Other software

| Software | Licence |
|---|---|
| Ollama | MIT |

Licences are summarised for convenience; the upstream licence texts are authoritative.
