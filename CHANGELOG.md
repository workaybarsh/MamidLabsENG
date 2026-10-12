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
- **Audio transcription (Whisper + audio events):** an attached audio file or microphone recording
  is transcribed automatically and the findings are given to the language model (the OCR/mmproj
  pattern). The engine is **faster-whisper large-v3**: highest accuracy + **Silero VAD** to suppress
  hallucinated text on silence. **Non-speech audio events** (AudioSet/AST; e.g. footsteps, a door)
  and optional **speaker diarization** (pyannote) are merged into the same transcript. The output is
  **chronological and timestamp-free**:
  ```
  (footsteps)
  Speaker: Did you come?
  (door closing)
  Speaker: Yes, a bit late.
  ```
  Fast on GPU (falls back to CPU). On the local provider the language model is unloaded from VRAM,
  the transcript is taken, then it is restored. A **microphone** button left of Send (**live
  recording from the Windows default input device**, dependency-free) and **Add audio** in the "+"
  menu. The engine and models are installed into `venv\asr` + the HF cache during installation.
- **ASR preempts the language-model load:** when a message with an audio attachment is sent while
  the language model is not ready, the job starts immediately; the in-flight model load is
  abandoned, the audio is transcribed with Whisper, then the language model (per the selected
  profile) is restored. So there is no need to wait for the model to load before sending audio.
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
- **Language profile (Profiles window):** the Profiles window gained a **UI language / language
  profile** selector (Turkish/English). From there the user can **download and install the other
  language edition's setup package**; it installs into the same folder (`UsePreviousAppDir`) and
  keeps `settings\` and `chats\`. The downloaded package is stored under `tools\editions` and can be
  **deleted** with "Delete language edition".

### Changed
- **Audio transcription CPU/GPU choice is now measurement-based:** the device is chosen
  automatically from the real free VRAM + system RAM + the recording **duration**. Measured
  threshold **PRO ~20 s**, **LITE ~15 s**; shorter recordings use the CPU, longer ones the GPU
  (swap). When the model + audio pipeline fit together in free VRAM the GPU is always used
  (no swap).
- **FLASH output cap 4096 -> 8192:** all three profiles (`--predict` and the UI `max_tokens`) now use
  8192. This is a **ceiling**, not a target: generation stops at EOS, so normal answers keep the same
  speed; it only prevents long answers/thinking from being cut off (empty reply). The MAX/FLASH
  distinction is carried by `--reasoning-effort` (xhigh vs low).
- **The audio transcription engine changed:** Qwen3-ASR and whisper.cpp were removed in favour of a
  **faster-whisper large-v3 + AST audio-events** pipeline (`agent/transcriber/`). The microphone menu
  gained a **"Non-speech sounds"** checkbox (unchecked -> speech only). **Clicking the red microphone
  button now cancels the recording** (stops it, deletes the file, sends nothing); use the right-click
  menu or **Send** to finish and send.
- **The setup wizard offers a Turkish/English language choice** (default English in this edition).
- **LITE no longer uses a thinking budget (unlimited):** `--reasoning-budget` is never passed;
  thinking stays on and the model thinks as long as it needs. The system prompt tells it to think
  deeply and not to cap its thinking time. The former 300-token budget was removed.

### Fixed
- **The transcript wrapper leaked on a silent/empty recording:** the Qwen3-ASR output parser
  (`_extract_text`) sent the raw `language None<asr_text>` text to the model as the transcript when a
  short recording contained no speech, so the model replied "the transcript looks empty". The
  `<asr_text>` tag and the `language ...` wrapper are now ALWAYS stripped; a silent result returns an
  **empty** text (caught in the UI test; verified with a real silent recording and a speech
  recording).
- **"Stop" now cancels EVERYTHING running at that moment instantly:** answer generation, long
  **subprocess work such as ASR/OCR/search** (Python subprocesses are killed immediately) and media
  generation stop without waiting; the language model is then loaded in the background and chat
  continues on the next message. **The one exception:** if the language model is **loading**, the
  load is not cut (it completes in the background).
- **Model-restore race after audio/OCR (8 GB LITE):** after the transcription/PaddleOCR subprocess
  exits, WDDM may release its VRAM late, so a language model started immediately could be rejected
  for "not enough memory" and stay down. The memory check is now retried a bounded number of times
  with short waits (~5 s); a single delayed release no longer takes the model down.
- **Microphone (audio recording) flow fixed:** clicking the **red microphone button** while
  recording now **finishes and sends** the recording (before it only opened the menu and nothing was
  sent). **"Cancel"** now really cancels the recording (the file is deleted and nothing is sent);
  **right-clicking** the microphone button opens the menu so it can be cancelled while recording.
  Pressing **Send** while recording also finishes and sends the recording.
- **"Stop" works properly after audio is sent:** transcription and the language-model restore wait
  now honor the cancel request; the UI no longer stays in "Stopping" for a long time.
- **Profile-aware ComfyUI lifecycle:** on MAX/FLASH (27B) profiles, ComfyUI is always closed before the
  language model loads (otherwise on a 16 GB card the MTP draft spills and chat intermittently drops to
  ~6-7 tok/s); on LITE ComfyUI stays open with no reopen between media jobs.
- **The `custom` (OpenAI-compatible endpoint) provider did not work in chat:** the base address only
  lived in the UI settings; the worker does not read settings, so `custom` requests stopped with
  "enter a base address" (only verification in the settings window worked). The desktop now passes
  `baseUrl` plus settings to the worker; verified end to end with commandcode (text + image).
