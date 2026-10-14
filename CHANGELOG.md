# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and version numbers
follow [Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-10-10

First release. A fully local multimodal AI workspace for Windows: a single WinForms window offers
chat, web search, document reading/creation, OCR, image understanding, image generation, photo
editing, video generation and chart drawing. The language model runs on `llama.cpp`
(`llama-server`), the image/video models on `ComfyUI`, on the user's NVIDIA graphics card. OpenAI,
Claude, Gemini, DeepSeek and custom OpenAI-compatible endpoints are optional API providers.

### Changed
- **LITE vision projector moved to the CPU (`--no-mmproj-offload`); it no longer counts toward the
  LITE VRAM budget.** Measured 7885→7249 MiB used (72→708 MiB free); chat speed unchanged.
- **Audio-transcription tier is now based on the selected profile:** the level of the local ASR
  pipeline (LITE: `int8_float16` + sequential loading + 15 s CPU/GPU threshold; PRO (MAX/FLASH):
  `float16` + parallel + 20 s) is now determined by the **selected language-model profile**;
  previously only total VRAM was considered. Total VRAM is now only a fallback metric when no
  profile name is given (CLI). The device/swap decision is still made from the measured real free
  VRAM + system RAM and the recording's duration.
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
- **Unprompted document/image/video/chart generation is blocked; a failed web search is reported
  honestly.** The generation tools were offered to the model on every turn and the only guard was
  the system prompt, so the small model (LITE) could generate unprompted. `create_document`,
  `generate_video` and `generate_chart` now get the same **call-time intent guard** as
  `generate_image` (`Test-DocumentRequestIntent` / `Test-VideoRequestIntent` /
  `Test-ChartRequestIntent`): if the user did not explicitly ask, the tool is not executed. Also,
  when a web search fails and the answer does not mention it at all, an honest note is appended
  (`Add-WebFailureNote`); `read_document` is now offered only with an attached document or a file
  intent (the model would invent a non-existent file and try to read it). With the internet off, a
  current-information answer (weather, news, price, "latest"…) also gets an honest note
  (`Add-NoInternetNote`). Regression tests: `tests/test_generation_intent_guard.py`.
- **"Delete all" no longer deletes the chat that is currently OPEN.** In the Past chats window
  "Delete all" used to wipe the whole list, including the chat on screen. The open chat's file is
  now excluded from the delete set and the confirmation says "The open chat is kept."; the open
  chat stays in history. The decision is made by the pure, testable `Get-DeletableChatPaths`
  helper (`tests/test_chat_history_delete.py`).
- **Audio attachment + Agent chip: the file tools are no longer offered to the model.** On a turn
  with an audio attachment the audio is already transcribed by faster-whisper into text, yet with
  the Agent chip on the small model could still try to find/read/write the audio FILE with
  `list_bound_files`/`read_bound_file`/`write_bound_file` and waste turns (seen in a UI test). Those
  three tools are now not offered on an audio turn (unless the user explicitly asks for a file), and
  a successful transcript note tells the model not to open the file with tools and to base its
  answer directly on the transcript.
- **Install/update correctness: script installs record `DisplayVersion` (and the in-app updater reads
  it); owned program dirs are pruned on update; `requirements\` is placed at the install root; the
  script copy matches the `.iss` excludes; repeated `layout` no longer adds a duplicate uninstall
  entry; the uninstaller removes `EULA.txt` and `requirements\`.**
- **Version/updater hardening and the wizard's nested source (`<root>\setup-source`) layout: the
  source-vs-root guard is equality-only so a fresh install lays down all program files.**
- **Empty visible-answer regression on hard questions (thinking consumed the whole output budget).**
  In an end-to-end math test LITE (9B), on a hard integral question, spent **all 8192 tokens**
  (~159.7 s) thinking and produced **no visible answer**; the bubble kept only "İntegral"
  (`logs\qwen-lite.stderr.log`: `eval 159714 ms / 8192 tokens`). Root cause: thinking is “unlimited”,
  the output cap `--predict 8192` and the UI `maxTokens 8192` **share the same budget**; the
  “double-check the steps” instruction made thinking even longer, and the existing empty-answer
  fallback was also stuck at the same 8192. (1) **Thinking-off recovery:** the new
  `Set-NoThinkingRequest` (`chat_template_kwargs = @{ enable_thinking = $false }`, plus `/no_think`)
  makes the second attempt turn thinking **fully off** (LITE ignores `reasoning_effort`, so this is
  the only guaranteed way) and **guarantees** a visible answer; the trigger now covers both an
  **empty** answer and one **cut off at the output cap** (`finishReason='length'`) leaving a broken
  fragment. (2) **System prompt softened:** “check at least TWICE” → “review the critical steps again
  **but also always write your answer**; do not think endlessly and end up with no answer” (the LITE
  deep-thinking line follows the same direction). (3) Since thinking is unlimited, raising `--predict`
  does not guarantee an answer and only lengthens thinking and latency (measured: 8192 tokens =
  159.7 s); so the guaranteed recovery was chosen and no KV/context overflow was introduced.
- **Block LaTeX is now rendered as readable text.** `Convert-MathText` handles the block structures a
  plain regex could not, with a **balanced parser** (`Read-MathGroup`/`Read-MathAtom`/`Expand-MathCommands`):
  nested/parenthesised and escaped `\{ \}` `\frac{A}{B}`, `\sqrt{X}` / `\sqrt[n]{X}`, `\boxed{X}`/`\fbox{X}`
  (`[ X ]` instead of a box), `\textbf`/`\textit`/`\mathrm`/`\text`/`\operatorname` (content),
  `\left`/`\right` (plain delimiters), `\overline`/`\bar`/`\vec`/`\hat` (combining marks) and
  `\begin`/`\end`. Example: `\boxed{\frac{\pi^5(89+72\sqrt3)}{3888}}` → `[ (π⁵(89+72√(3)))/3888 ]`.
  The existing glyph table, the chemistry `\ce`/`\pu` support and the currency `$` behavior are kept.
- **A fixed "evidence-based and double-check" instruction was added to the system prompt.**
  `Get-SystemPrompt` (not per-turn; the KV cache is not broken) now tells the model to give correct,
  evidence-based answers, to check the steps and the symbolic/numeric **intermediate results at least
  twice** in multi-step math/algebra/logic, and **not to invent test/theorem/source names**; the LITE
  deep-thinking line was merged with it (no duplication).
- **Audio: the LITE CPU/GPU policy and the GPU runtime DLLs were clarified; the transcribe timeout
  is now tied to the recording duration.** (1) LITE ASR now uses the **CPU co-resident** path
  (`('cpu', False)`; the language model stays in VRAM, the audio model runs on the CPU/system RAM,
  no swap) ONLY when the recording is **shorter than** `LITE_CPU_MAX_SECONDS` (15 s) **AND** system
  RAM suffices; for a long recording or insufficient RAM it uses **GPU + mandatory swap**
  (`('cuda', True)`). The CPU path is never a forced choice: if the chosen device fails it falls
  back to the CPU, and it remains the last resort when there is no GPU. PRO behavior is unchanged
  (20 s). **Measurement verdict** (two independent measurements conflicted; the more DEFINITIVE
  one, measurement-2, actually ran the LM and the ASR CPU pipeline TOGETHER): LITE co-resident
  **DOES NOT FIT** — physical RAM 15.25 GB < requirement ~16.1 GB; zero/negative margin (peak
  ~15127/15611 MB, only ~489 MB free); there is no OOM, but the LM's ~6.1 GB model pages are
  evicted from the reclaimable cache and **re-read from disk on the next chat turn**. So the LITE
  `audio_ram_mib` is kept at **5120 MiB** (the SAME safe gate as PRO); with the LM loaded
  `choose_plan` does NOT pick the CPU branch -> **GPU + swap** (user rule: fits in RAM ->
  co-resident, does not fit -> VRAM swap). On a RAM-rich machine a short recording can still use
  the CPU co-resident path. (2) `requirements\asr.txt` gained `nvidia-cublas-cu12` + `nvidia-cudnn-cu12` (CTranslate2
  4.x on Windows looks for `cublas64_12.dll` / `cudnn_*64_9.dll`; torch's cu130 wheels ship
  `cublas64_13.dll`) and `asr.py` `_add_cuda_dll_dirs` adds `site-packages\nvidia\*\bin` to the DLL
  search path; **GPU transcription now works** (verified with a real recording, `device=cuda`).
  (3) The transcribe request now carries a finite `timeoutSeconds` derived from the recording
  duration (`max(600, d*4+300)`, capped at 3600; 1800 when unknown) so a long CPU co-resident/swap
  transcription is not cut off at the fixed 180 s; "Stop" still takes effect instantly.
  `tests/test_transcriber.py` and `tests/test_requirements_files.py` guard these contracts. The
  swap status text was also fixed: the language model is not moved to RAM, `manage-qwen -Action
  stop` **stops the process** (no code behavior change).
- **Audio transcription: the CPU fallback never ran because of a PowerShell dictionary bug (found
  in the end-to-end audio test).** When the primary device (GPU) failed, `Invoke-TranscribeWithModelSwap`
  built the CPU retry payload with `$payloadBase + @{ device = 'cpu' }`; in PowerShell a Hashtable
  `+` **throws on a duplicate key** ("Item has already been added. Key in dictionary: 'device'")
  because `$payloadBase` already contains `device`/`need_swap`. So the worker never actually fell
  back to CPU, the real ASR error was swallowed, and the turn finished without a transcript (e.g.
  when `cublas64_12.dll` required by ctranslate2 is missing, the GPU path always errors). The
  payload is now cloned and the field overwritten (`$cpuPayload = $payloadBase.Clone();
  $cpuPayload['device'] = 'cpu'`); the same fix applies to the "unload the model and retry" block.
  `tests/test_transcriber.py` guards this pattern.
- **Hygiene: audit-log rotation and a thinking (reasoning) repeat guard (F3/F4).** (F3) The tool-call
  record `logs\audit.jsonl` grew without bound; now, before a write, if the file exceeds 5 MB it is
  rotated to `audit.1.jsonl` (overwriting any previous one) and the new line goes to the clean
  file — the rotation is inside the existing `try/catch`, so audit writing never fails a turn. (F4)
  The runaway-repeat detector looked only at the visible content; because thinking is **unlimited**
  on LITE, if the model started repeating in the `reasoning_content` stream it spun to the
  8192-token cap with no visible answer. The **same threshold/pattern** is now applied
  conservatively to the reasoning accumulation too; on strong repetition the stream is stopped at a
  safe point (if there is no visible answer the existing empty-answer path is kept).
- **Agent-loop resilience: in-turn context overflow, wall-clock limit and `custom`-provider
  tolerance.** (F1) Tool results accumulating during a turn could push the prompt past
  `contextLimit`/`--ctx-size` and, because of `--no-context-shift`, the server returned an error and
  the turn failed; now at the start of every turn the estimated prompt size is computed and, if the
  budget is exceeded, the **oldest tool outputs** are shortened (keeping the system message and the
  last user message); if it still does not fit the model is asked for a visible answer from the
  findings without continuing with tools. (F2) `max_tokens` is capped to the remaining context room
  (small-context model/endpoint). (D2) Python subprocesses now have a wall-clock limit (default
  180 s; killed when exceeded). (D3) An OpenAI-compatible `custom` endpoint retries once on a
  transient drop and does not treat a drop as an error when a response was produced; remote
  providers keep their behavior.
- **When an attached audio file could not be transcribed the model tried to read it and wasted the
  turns (found in an end-to-end audio test).** When an attached `.wav` produced no transcript the
  worker silently continued "without a transcript" and the `[The user attached an audio file: ...]`
  note in the user message was left alone; the model took it for an "attached document", tried to
  open the `.wav` with `read_document`/`read_bound_file` and spent **8/8 agent turns** without ever
  producing a transcript. Now (1) if the plan says "no swap" but no transcript is obtained, the
  language model is unloaded and retried once with a **clean GPU/RAM** (under WDDM the free VRAM the
  plan reads can be optimistic while the model is loaded), and (2) if it still fails an explicit
  note is added to the last user message saying the audio is **not a document** and could not be
  transcribed. The engine itself was sound (calling `whisper-tools.py` directly works); the flaw was
  the worker swallowing the failure and leaving the model with the wrong context.
- **`EULA.txt` was not copied by script-based updates:** the `install-mamidlabs.ps1` upper-file
  list (`qwen-desktop.ps1`, `launch-qwen-desktop.vbs`, `README.md`, `LICENSE`) is copied to the root
  and to the `setup-source` package; because `EULA.txt` was not in the list, the license file was
  missing from installs updated by script (without the wizard). It was added to the list.
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
  ~6-7 tok/s); on LITE it is closed by default and can be kept open between media jobs with
  `comfyKeepAlive=true`.
- **The `custom` (OpenAI-compatible endpoint) provider did not work in chat:** the base address only
  lived in the UI settings; the worker does not read settings, so `custom` requests stopped with
  "enter a base address" (only verification in the settings window worked). The desktop now passes
  `baseUrl` plus settings to the worker; verified end to end with commandcode (text + image).

### Docs
- **Documentation/comment consistency pass (no behavior change).** Stale or hard-coded figures and
  missing items were fixed: `packaging/windows/mamidlabs.iss` (the header no longer hard-codes
  `v1.0.0`), `AGENTS.md` (the `asr` install stage, `requirements/asr.txt` in the repo map, the
  `chart` smoke step, the real test figures, the reconciled MTP acceptance `~0.93 (content-dependent
  0.31–0.96)`, and an explicit "at that time" framing for the 7885/8188 MiB LITE row),
  `docs/installation.md` (the `asr` stage), `docs/technical-notes.md` (MTP acceptance and the LITE
  margin wording), `scripts/manage-qwen.ps1` (a single LITE size figure, `~6.5 GB + ~0.46 GB MTP
  head`), `README.md` (a "For Türkçe" link) and the `agent/transcriber` package-version comment.

### Added
- **Chat (local + API):** local chat via `llama-server` (MAX/FLASH/LITE profiles; Qwen3.8 27B and
  Qwen3.5 9B), MTP (Multi-Token Prediction) speculative decoding for ~2x speed (no separate draft
  model, lossless output). OpenAI, Claude, Gemini, DeepSeek and custom OpenAI-compatible endpoints.
- **Media generation (local, ComfyUI):** image from text (Krea-2 Turbo), regenerate from an image,
  style reference, identity-preserving photo editing (Krea 2 Identity Edit; MAX/FLASH) and video
  generation (Wan 2.2 TI2V 5B). By default ComfyUI runs only during media generation (it is closed
  when the job ends and holds neither VRAM nor RAM while idle); on LITE it can be kept open between
  media jobs with `comfyKeepAlive=true`. A leaked/duplicate instance is cleaned before loading.
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
