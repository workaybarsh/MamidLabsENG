# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and version numbers
follow [Semantic Versioning](https://semver.org/).

## [1.0.1] - 2026-10-11

### Fixed
- **Microphone (audio recording) flow fixed:** clicking the **red microphone button** while
  recording now **finishes and sends** the recording (before it only opened the menu and nothing was
  sent). **"Cancel"** now really cancels the recording (the file is deleted and nothing is sent);
  **right-clicking** the microphone button opens the menu so it can be cancelled while recording.
  Pressing **Send** while recording also finishes and sends the recording.
- **"Stop" works properly after audio is sent:** transcription and the language-model restore wait
  now honor the cancel request; the UI no longer stays in "Stopping" for a long time.

### Added
- **ASR preempts the language-model load:** when a message with an audio attachment is sent while
  the language model is not ready, the job starts immediately; the in-flight model load is
  abandoned, the audio is transcribed with Qwen3-ASR, then the language model (per the selected
  profile) is restored. So there is no need to wait for the model to load before sending audio.

## [1.0.0] - 2026-10-10

First release. A fully local multimodal AI workspace for Windows: a single WinForms window offers
chat, web search, document reading/creation, OCR, image understanding, image generation, photo
editing, video generation and chart drawing. The language model runs on `llama.cpp`
(`llama-server`), the image/video models on `ComfyUI`, on the user's NVIDIA graphics card. OpenAI,
Claude, Gemini, DeepSeek and custom OpenAI-compatible endpoints are optional API providers.

### Added
- **Chat (local + API):** local chat via `llama-server` (MAX/FLASH/LITE profiles; Qwen3.8 27B and
  Qwen3.5 9B), MTP (Multi-Token Prediction) speculative decoding for ~2x speed (no separate draft
  model, lossless output). OpenAI, Claude, Gemini, DeepSeek and custom OpenAI-compatible endpoints.
- **Media generation (local, ComfyUI):** image from text (Krea-2 Turbo), regenerate from an image,
  style reference, identity-preserving photo editing (Krea 2 Identity Edit; MAX/FLASH) and video
  generation (Wan 2.2 TI2V 5B). ComfyUI is kept open between media jobs and runs together with the
  language model as a single healthy instance (measured 61-62 tok/s); a leaked/duplicate instance is
  cleaned before loading.
- **Documents and OCR:** read PDF/Word/Excel/PowerPoint/ZIP/RAR, create PDF/DOCX/PPTX; image and PDF
  OCR with PaddleOCR.
- **Audio transcription (Qwen3-ASR-1.7B):** an attached audio file is transcribed automatically and
  the findings are given to the language model (the OCR/mmproj pattern). The engine is **Qwen3-ASR**:
  52 languages + **Turkish**; speech, singing and music-with-BGM; fast on GPU (falls back to CPU).
  On the local provider the language model is unloaded from VRAM, the transcript is taken, then it is
  restored. A **microphone** button left of Send (**live recording from the Windows default input
  device**, dependency-free) and **Add audio** in the "+" menu. The model is downloaded into
  `models\qwen3-asr` during installation.
- **Dependency-free chart generation:** the `generate_chart` chat tool + `agent/chart-run.ps1`
  (bundled pwsh + System.Drawing; bar/line/pie/scatter/function charts; a safe expression evaluator,
  no `Invoke-Expression`). The result is shown inline in the chat and saved under `outputs\charts`.
- **Math rendering:** LaTeX answers are turned into readable Unicode (∫, ∞, π, √, ∑, →, ≠, Greek
  letters; fractions → a/b; exponents/subscripts → Unicode). Also **chemistry/general-science**
  glyphs: equilibrium arrow ⇌, ↔ ⟷ ⟹ ↪ ↑ ↓ ↕, and **mhchem `\ce{...}`/`\pu{...}`** (element numbers
  become subscripts, arrows become arrows). A currency `$` is kept.
- **Web and local management:** web search and page reading tools; browser-accessible local
  management server (localhost only, password-protected session).
- **Installation and updates:** Inno Setup wizard; pinned infrastructure (PowerShell 7.6.6, llama.cpp
  b10998 CUDA 13.4, ComfyUI b0f4b7b); in-app update check; full uninstall.
  In setup, **image/video generation, audio capture and OCR are optional** (checked by default;
  unchecking installs only the chat/document tools). The same components can be selected, downloaded
  or deleted from the **Profile Manager**.
- **License (EULA):** the setup wizard shows an **End User License Agreement** and requires accepting
  it. Ownership and all rights belong to the developer; copying, modifying, cracking or commercially
  marketing the Software is prohibited. Unauthorized use may carry civil and criminal liability;
  disclaimer and limitation of liability, termination and governing law/jurisdiction are included.

### Fixed
- **Profile-aware ComfyUI lifecycle:** on MAX/FLASH (27B) profiles, ComfyUI is always closed before the
  language model loads (otherwise on a 16 GB card the MTP draft spills and chat intermittently drops to
  ~6-7 tok/s); on LITE ComfyUI stays open with no reopen between media jobs.
- **The `custom` (OpenAI-compatible endpoint) provider did not work in chat:** the base address only
  lived in the UI settings; the worker does not read settings, so `custom` requests stopped with
  "enter a base address" (only verification in the settings window worked). The desktop now passes
  `baseUrl` plus settings to the worker; verified end to end with commandcode (text + image).
