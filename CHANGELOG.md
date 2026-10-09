# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and version numbers
follow [Semantic Versioning](https://semver.org/).

## [1.0.7] - 2026-10-09

### Fixed
- **The VRAM threshold could wrongly reject the machine (a 1.0.6 regression):** in 1.0.6 the
  MAX/FLASH margin was raised to 1000 MiB, making the threshold 15600 MiB, which even the
  machine's normal free value (15442–15558 MiB) fell below, raising "Not enough free VRAM"
  (the chat and UI smoke steps failed). Measured: the model needs ~15422–15442 MiB on the GPU;
  with ComfyUI open free VRAM drops to ~15020 MiB. The margin is now **800 MiB** (threshold
  15400): ComfyUI is closed when open, and the model loads with ComfyUI closed / on a cold
  boot. The same fix is applied to the Turkish edition.

## [1.0.6] - 2026-10-09

### Added
- **UI smoke step (`ui`):** the smoke test now launches the real desktop app (`qwen-desktop.ps1`)
  and verifies the window and the language model are ready; it then runs one turn with the real
  worker the UI uses (`agent/qwen-inference.ps1`) and proves there is **no "connection
  dropped"/watchdog text** and that **MTP really works** (`--spec-type draft-mtp` + draft
  acceptance). Because the app overwrites the settings on exit, the settings are backed up and
  restored (skippable with `-SkipUi`).

### Fixed
- **Root cause of the post-media drop to ~6 tok/s:** the VRAM budget under-counted the real load
  (model + 300 MiB). The 27B model takes ~**15,378–15,442 MiB** on a 16 GB card (32K context, q4_0
  KV, MTP draft context); with an idle ComfyUI (~300–460 MiB) it no longer fit fully and the MTP
  draft context (~185 MiB) silently moved to the CPU, dropping production from 44 tok/s to ~6–7
  tok/s (measured: llamaVRAM 15378 → 15193). The margin is now **1000 MiB** and the MAX/FLASH
  tolerance is **0**, so when free VRAM is short an idle ComfyUI is closed and the model fits
  fully every time. LITE is unchanged (margin 300, tolerance 1024).
- **`Release-Comfy` hardening:** even when ComfyUI `/free` unloads its torch cache, the CUDA context
  and driver-side reservation may still hold VRAM; the real free VRAM (`nvidia-smi`) is now
  verified and ComfyUI is closed when it is not enough (image/video workers pass the MAX/FLASH
  14000 MiB / LITE 6000 MiB threshold).
- **Missing update package:** script-based updating (`install-mamidlabs.ps1 -Stage layout`) did not
  refresh the wizard source package (`setup-source`); on a wizard-installed root it stayed at the
  previous version. It is now refreshed when present.

## [1.0.4] - 2026-10-08

### Added
- **Local web management:** the app UI can be managed from a browser (chat, model switching,
  generated files). It is reachable only from this computer (127.0.0.1) by default, is
  password protected (DPAPI, bound to the Windows account) and issues a single-use connect
  link that expires in 5 minutes. Exposing it to the local network needs an explicit setting;
  sessions last 30 days and are invalidated when the password changes. File access is confined
  to `outputs\`.
- Generated files (image/video/document) can be **opened in one click** from the chat bubble ("Open").

### Fixed
- **Model load failure ("Model işlemi başarısız"):** the MTP head grew the LITE model by
  ~464 MiB and the VRAM check refused to start it on an 8 GB card. The check now tolerates a
  small overflow (llama.cpp keeps the big embedding tensor on the CPU) and stops only on a clear one.
- **Private repository update check:** "Check for updates" returned 404; it now uses a GitHub
  token (GITHUB_TOKEN/GH_TOKEN or the "Update token" field in the Profiles window).

## [1.0.3] - 2026-10-08

### Added
- **MTP (Multi-Token Prediction) speculative decoding for LITE.** The MTP/nextn head dropped in the
  conversion of the Huihui-Qwen3.5 9B GGUF is added automatically on download
  (`agent/mtp_inject.py`); no separate draft model is needed and the output is lossless. Measured
  acceptance ~0.93, ~2x speed (36 → ~75 tok/s). Disable with `settings/api.json` → `"mtp": false`.

### Changed
- License changed from MIT to a proprietary (all rights reserved) license; copyright is held by
  Aybars Haydar Altuntaş.

## [1.0.2] - 2026-10-08

### Added
- **MTP (Multi-Token Prediction) speculative decoding** enabled for MAX/FLASH
  (`--spec-type draft-mtp`). The Qwen3.8 27B GGUF carries a built-in MTP/nextn draft head, so no
  separate draft model is needed; output is **lossless** (accepted tokens are verified against the
  target model) and ~1.2–1.5x speed is expected. The LITE (Qwen3.5 9B) model has no MTP layers, so
  it is not used there. Disable with `settings/api.json` → `"mtp": false`.

### Notes
- Verified that MTP does not change the output: with greedy decoding (temperature 0) and a fixed
  seed, the output with and without MTP is byte-identical.

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

[1.0.4]: https://github.com/workaybarsh/MamidLabsENG/releases/tag/v1.0.4
[1.0.3]: https://github.com/workaybarsh/MamidLabsENG/releases/tag/v1.0.3
[1.0.2]: https://github.com/workaybarsh/MamidLabsENG/releases/tag/v1.0.2
[1.0.1]: https://github.com/workaybarsh/MamidLabsENG/releases/tag/v1.0.1
[1.0.0]: https://github.com/workaybarsh/MamidLabsENG/releases/tag/v1.0.0
