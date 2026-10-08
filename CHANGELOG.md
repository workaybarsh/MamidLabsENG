# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and version numbers
follow [Semantic Versioning](https://semver.org/).

## [1.0.1] - 2026-10-07

### Added
- Offline operation tests: with the internet chip on but the network down, the web search,
  page reading and reference image tools do not raise; they return valid error JSON to the
  model (see `tests/test_offline_mode.py`).
- Added an **`offline`** step to the smoke test: after installation, network access is cut
  temporarily to prove the web tool returns error JSON without crashing.
- Added a **`network_switch`** step to the smoke test: across successive turns the network goes
  online/offline (internet coming and going) and the local worker must not error in any turn.
- Added a **`history_resume`** step to the smoke test: closing the program mid-conversation and
  reopening it, the chat is saved, reloaded and continued; no turn errors. The step guards against
  `$done.data` coming back as `$null` across the thread boundary when no tool was called
  (otherwise a blank message was appended and the server returned
  `400 All non-assistant messages must contain 'content'`).
- **MTP (Multi-Token Prediction) speculative decoding** enabled for MAX/FLASH
  (`--spec-type draft-mtp`). The Qwen3.8 27B GGUF carries a built-in MTP/nextn draft head, so no
  separate draft model is needed; output is lossless and ~1.2–1.5x speed is expected. The LITE
  (Qwen3.5 9B) model has no MTP layers, so it is not used there. Disable with `settings/api.json`
  → `"mtp": false`.

### Fixes
- Fixed the intermittent **"An existing connection was forcibly closed by the remote host."**
  (WSAECONNRESET) error seen during local model streaming. The server can close a keep-alive
  connection that sits idle while an agent tool runs (web search, OCR, document reading); when the
  client reused that stale connection the stream failed mid-answer. Local requests now open a fresh
  connection every turn (`Connection: close`; negligible on loopback, and the server-side KV cache
  and prefill savings are preserved). When a **usable result** exists (finish_reason received,
  answer text present, or a tool call present), a drop is no longer treated as an error, so a
  completed answer is not given a spurious "[Error]" line.
  (`agent/qwen-inference.ps1`)
- **Stopped the model from "working around" a failed web tool.** When the network is down and web
  search / page download fails, the model used to try to get the result another way with
  `run_command` (ping/curl/wget) or to make it up. Such a failure is now detected and a system note
  is appended to the request copy (never written to the persistent history): it forbids workarounds,
  asks for a clear answer from existing knowledge, and to otherwise state that web access could not
  be established without inventing the result. A preventive rule was added to the system prompt too.
  Two hard guards: after a web failure (1) web tools are no longer offered to the model, and (2)
  `run_command` refuses network-probe commands (ping/curl/wget/nslookup/ipconfig…) **without running
  them** (a small model may skip the note).
- **`run_command` is now offered to the model only when the user explicitly asks for a command or a
  terminal** (`Test-CommandIntent`). So the model cannot even attempt to "work around" a web failure
  with commands; on general questions the command tool is not offered and cannot be called. Explicit
  intent patterns: command, terminal, console, cmd, powershell, bash, script/batch,
  .bat/.cmd/.ps1/.exe, run, execute, ipconfig, ping, curl, wget, nslookup, netstat.
  (`agent/qwen-inference.ps1`, `qwen-desktop.ps1`)

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
