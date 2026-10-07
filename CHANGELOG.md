# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and version numbers
follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Offline operation tests: with the internet chip on but the network down, the web search,
  page reading and reference image tools do not raise; they return valid error JSON to the
  model (see `tests/test_offline_mode.py`).
- Added an **`offline`** step to the smoke test: after installation, network access is cut
  temporarily to prove the web tool returns error JSON without crashing.

## [1.0.1] - 2026-10-07

### Fixes
- Fixed the intermittent **"An existing connection was forcibly closed by the remote host."**
  (WSAECONNRESET) error seen during local model streaming. The server can close a keep-alive
  connection that sits idle while an agent tool runs (web search, OCR, document reading); when the
  client reused that stale connection the stream failed mid-answer. Local requests now open a fresh
  connection every turn (`Connection: close`; negligible on loopback, and the server-side KV cache
  and prefill savings are preserved). When answer text has already been received, a drop is no
  longer treated as an error, so a completed answer is not given a spurious "[Error]" line.
  (`agent/qwen-inference.ps1`)

## [1.0.0] - 2026-09-27

First stable release.

### Application
- Chat, web search, document reading and creation, OCR, image understanding, image generation,
  photo editing and video generation in a single window.
- Three hardware profiles: **MAX** and **FLASH** (Qwen3.8 27B, 16 GB VRAM) and **LITE** (Qwen3.5
  9B, 8 GB VRAM). The Profiles menu shows install status, downloads missing files and switches
  profiles.
- Image generation with Krea-2 Turbo; regeneration, style reference and a realism LoRA on request.
  Photo editing with Krea 2 Identity Edit (16 GB profiles only).
- Video generation with Wan 2.2 TI2V 5B: from text, from an image and continuing from a video.
- Optional API providers: OpenAI, Claude, Gemini, DeepSeek and a custom OpenAI-compatible endpoint.
- Use your own language, image and video models by file path; files are not copied.
- Agent mode: reading/writing files in a bound folder and running commands, with user approval.
- In-app update check (Profiles → Check for updates); an update opens the setup wizard and closes
  the app.

### Installation
- One-click setup wizard (`MamidLabs_Setup.exe`): profile recommendation from a hardware
  measurement, option to download models later, own model/API account option, live progress,
  cancel with rollback, optional smoke test and a summary at the end.
- Default install folder `%LOCALAPPDATA%\Programs\MamidLabs`; no administrator rights needed.
- Components are installed at pinned versions: PowerShell 7.6.6, llama.cpp b10998 (CUDA 13.4),
  ComfyUI `b0f4b7b`; the Python packages of the app and media environments are locked. Model files
  are verified against a commit and byte size.
- Missing prerequisites (Python, Git, 7-Zip, VC++ Redistributable) are installed with winget.
- Full uninstall, including models, caches, chats and the prerequisites MamidLabs installed.

### Security
- Writing files, running commands and creating documents require approval by default; if approval
  is not given within 10 minutes the action is rejected.
- Tool calls are written to the `logs\audit.jsonl` audit log; chats and API keys are encrypted with
  Windows DPAPI.
- Web tools only reach public HTTP(S) addresses; DNS rebinding protection is in place.

[1.0.1]: https://github.com/workaybarsh/MamidLabsENG/releases/tag/v1.0.1
[1.0.0]: https://github.com/workaybarsh/MamidLabsENG/releases/tag/v1.0.0
