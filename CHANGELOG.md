# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and version numbers
follow [Semantic Versioning](https://semver.org/).

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
- **Audio transcription (whisper.cpp):** an attached audio file is transcribed automatically and the
  findings are given to the language model (the OCR/mmproj pattern). Model **large-v3-turbo-q5_0 +
  Silero VAD** (hallucination suppressed). Measured: 2 min audio ~53 s on CPU, ~2.5 s on GPU (CUDA).
  On the local provider the language model is unloaded from VRAM, the transcript is taken, then it is
  restored. A **microphone** button left of Send and **Add audio** in the "+" menu.
- **Dependency-free chart generation:** the `generate_chart` chat tool + `agent/chart-run.ps1`
  (bundled pwsh + System.Drawing; bar/line/pie/scatter/function charts; a safe expression evaluator,
  no `Invoke-Expression`). The result is shown inline in the chat and saved under `outputs\charts`.
- **Math rendering:** LaTeX answers are turned into readable Unicode (∫, ∞, π, √, ∑, →, ≠, Greek
  letters; fractions → a/b; exponents/subscripts → Unicode). A currency `$` is kept.
- **Web and local management:** web search and page reading tools; browser-accessible local
  management server (localhost only, password-protected session).
- **Installation and updates:** Inno Setup wizard; pinned infrastructure (PowerShell 7.6.6, llama.cpp
  b10998 CUDA 13.4, ComfyUI b0f4b7b); in-app update check; full uninstall.

### Fixed
- **The `custom` (OpenAI-compatible endpoint) provider did not work in chat:** the base address only
  lived in the UI settings; the worker does not read settings, so `custom` requests stopped with
  "enter a base address" (only verification in the settings window worked). The desktop now passes
  `baseUrl` plus settings to the worker; verified end to end with commandcode (text + image).
