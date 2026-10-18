# Technical notes

This document describes the inner workings of MamidLabs, profile details, the media pipeline, the
tools and known notes. For installation see [installation.md](installation.md), for an overview the
[README](../README.md).

**Install path.** `%LOCALAPPDATA%\Programs\MamidLabs` in this document is the **default** location, not a
fixed requirement. The app runs from its own location; it can be installed into any SSD folder by
choosing the folder in the wizard or with `install-mamidlabs.ps1 -Root <path>`.

## Architecture

```
mamidlabs-desktop.ps1  (WinForms interface, single window)
      │
      ├─ agent/mamidlabs-inference.ps1   → llama-server /v1/chat/completions (stream + tool loop)
      │       ├─ agent/mamidlabs-agent-tools.py  → web search, page reading, finding reference images
      │       ├─ agent/office-tools.py      → read/write PDF / Word / Excel / PowerPoint
      │       ├─ agent/ocr-tools.py         → PaddleOCR on the GPU for plain images, on the CPU for PDFs
      │       └─ agent/rag-tools.py         → offline BM25 search in the bound folder (search_folder)
      │
      ├─ agent/imagegen-run.ps1     → VRAM swap + ComfyUI + Krea-2 Turbo
      ├─ agent/videogen-run.ps1     → VRAM swap + ComfyUI + Wan 2.2 TI2V-5B
      │       └─ agent/media-tools.py       → video frame extraction, thumbnails
      │
      ├─ agent/media-model-swap.ps1 → VRAM handover, ComfyUI lifecycle
      ├─ scripts/manage-mamidlabs.ps1    → start / stop llama-server / switch profile, memory gate
      │       └─ agent/process-launch.ps1   → detaches long-lived processes from the caller's output pipe
      └─ agent/profile-catalog.ps1  → profile catalog, missing file scan, download
              └─ agent/profiles.json        → file list and sources per profile
```

| Component | Path |
|---|---|
| llama.cpp (CUDA) | `%LOCALAPPDATA%\Programs\MamidLabs\llama.cpp\b10998\llama-server.exe` |
| Language model | `%LOCALAPPDATA%\Programs\MamidLabs\models\Qwen3.8-27B-Abliterated-IQ4_XS\*.gguf` (+ mmproj for images) |
| Image/video models | `%LOCALAPPDATA%\Programs\MamidLabs\ComfyUI\models\{diffusion_models,text_encoders,vae,loras}` |
| Image/video environment | `%LOCALAPPDATA%\Programs\MamidLabs\venv\imagegen` (torch cu130, PyAV) |
| OCR environment | `%LOCALAPPDATA%\Programs\MamidLabs\venv\ocr` (RTX 50 sm_120 GPU PaddlePaddle + PaddleOCR) |
| PDF OCR environment | `%LOCALAPPDATA%\Programs\MamidLabs\venv\ocr-cpu` (standard CPU PaddlePaddle + PaddleOCR) |
| Web/document environment | `%LOCALAPPDATA%\Programs\MamidLabs\venv\app` (ddgs, pypdf, reportlab, python-docx, python-pptx) |
| PowerShell 7 | `%LOCALAPPDATA%\Programs\MamidLabs\runtime\pwsh\pwsh.exe` (bundled with the app) |
| Generated files | `%LOCALAPPDATA%\Programs\MamidLabs\outputs\{images,videos,documents,refs,thumbs}` |
| Chat history | `%LOCALAPPDATA%\Programs\MamidLabs\chats` |
| Logs | `%LOCALAPPDATA%\Programs\MamidLabs\logs` |

Launch: the **MamidLabs** shortcut on the desktop → `launch-mamidlabs-desktop.vbs`
(no console window opens; if the app is already open it is brought to the front).

## Profiles

| Profile | Model | Context | Reasoning | max_tokens | Measured speed |
|---|---|---|---|---|---|
| MAX (deep) | Qwen3.8 27B IQ4_XS | 32K | `xhigh` | 8192 | ~42–58 token/s |
| FLASH (fast) | Qwen3.8 27B IQ4_XS | 32K | `low` | 8192 | ~42-58 token/s |
| LITE (8 GB) | Huihui-Qwen3.5 9B Q5_K_M | 32K | unlimited (no budget) | 8192 | ~40–79 token/s (8 GB) · ~97 token/s (16 GB) |

MAX and FLASH share the same 27B file: 32K context (auto 16K fallback when 32K does not fully fit
VRAM, see the memory-budget section), `--gpu-layers 99`, flash-attn on, KV cache q4_0,
the image projector (mmproj) on the CPU (`--no-mmproj-offload`; on the GPU it did not fit on a 16 GB
card and spilled into shared memory). **If you do not change** the selector at the bottom right,
**the model is not reloaded**.

**MTP (Multi-Token Prediction).** MAX/FLASH start with `--spec-type draft-mtp` using the built-in
MTP/nextn draft head of the Qwen3.8 27B GGUF (`qwen35.nextn_predict_layers = 1`, `blk.64.nextn.*`).
The LITE 9B GGUF loses that head in the conversion, so it is re-injected on download by
`agent/mtp_inject.py` (source: the `huihui-ai/Huihui-Qwen3.5-9B-abliterated` MTP tensors); therefore
`--spec-type draft-mtp` is used on LITE too. No separate draft model is needed; output is lossless
(accepted tokens are verified against the target model) and it gives ~2x speed (acceptance on LITE
~0.93 (content-dependent 0.31–0.96); the model card reports 0.35–0.53 on Q4_K_M for the 27B). The flag is added only if the
GGUF really carries MTP layers (`Test-GgufHasMtp`); passing it to one that does not makes llama.cpp
fail with "model doesn't contain MTP layers". It can be disabled by putting `"mtp": false` in
`%LOCALAPPDATA%\Programs\MamidLabs\settings\api.json`.

### 16 GB memory budget (MAX/FLASH)

Measured on an RTX 5060 Ti 16 GB with llama.cpp's own buffer report (llama.cpp b10998, q4_0 KV, MTP on):

| Component | VRAM |
|---|---|
| Model weights (GPU part; `token_embd` stays on the CPU) | 13,945 MiB |
| KV cache (q4_0) | 288 MiB @16K · 576 MiB @32K |
| Recurrent state | 598.5 MiB (fixed; cannot be shrunk) |
| Compute buffer | 112 MiB |
| CUDA context | ~380 MiB |
| **Total footprint** | **~15.3 GiB @16K · ~15.8 GiB @32K** |

On the GPU the image projector asked for another 1,133 MiB with weights and compute buffer; it
exceeded the total free space and spilled ~420 MiB into shared memory. With the projector moved to
the CPU, chat went from 19.6 to 25.0 tok/s and reading a single image from 56–58 s to 29 s.

**A 32K context does not fit a normal desktop:** free VRAM with only the Windows desktop running is
~15.5 GiB while a 32K context needs ~15.8 GiB, so it spilled ~400 MiB into shared memory and chat
dropped from ~40 to ~27 tok/s; at 16K the footprint is 15,324 MiB, it fits and runs at ~40 tok/s.
The memory check in `scripts/manage-mamidlabs.ps1` is therefore **context-aware**
(`Get-ModelOverheadMiB`: 1200 MiB @32K, 800 @16K, 600 below) and MAX/FLASH **tries 32K first and
falls back to 16K** when 32K does not fully fit (same weights, half the KV cache). The chosen value
is used for `--ctx-size` too, and the UI reads the real context back from the server so its
compaction threshold matches. **ComfyUI lifecycle**
(`settings/api.json` → `comfyKeepAlive`, default `false`): ComfyUI runs **only during image/video
generation**; it is closed when the job ends and before the language model loads, so it holds
**neither VRAM nor RAM while idle** (an idle ComfyUI kept ~300 MiB of CUDA context plus RAM). It
reopens on demand (~27 s). On MAX/FLASH it is **always** closed before the language model loads; on
LITE it can be kept open between media jobs with `comfyKeepAlive=true` (then it does **not** spill —
measured: identical GPU buffer layout whether ComfyUI is open or closed; graph splits 2 — only chat
slows by ~11%, 49.7 → 44.2 tok/s). If memory is short (empty queue) ComfyUI is closed and measured
again.

**Automatic VRAM cleanup:** before the language model loads, leftover app-owned **GPU workers** are
closed too — an idle persistent audio worker (`whisper-tools.py --serve`) and the OCR worker
(`ocr-tools.py`); after a crash or forced close their remaining CUDA contexts (~140–600 MiB) can eat
the 27B's thin margin. `scripts/manage-mamidlabs.ps1` → `Get-AppGpuWorkers`/`Stop-OrphanWorkers` runs
**before** the memory gate, waits for the driver to release VRAM and retries the gate a bounded
number of times (~7 s). The cleanup only targets processes under the install root; other
applications are never touched.

### Lite profile (8 GB VRAM / 16 GB RAM)

Since 8 GB cards cannot carry the 27B model, Lite loads a separate 9B model and shrinks the whole
media pipeline:

| Area | MAX / FLASH | LITE |
|---|---|---|
| Language model | Qwen3.8 27B IQ4_XS (~15 GB) | Huihui-Qwen3.5 9B Q5_K_M (~6.6 GB) |
| Context | 32K | 32K |
| Image reading (mmproj) | F16, on the CPU | Q8_0, on the CPU |
| Image generation | Krea-2 Turbo (fp8, ~13.1 GB) | Krea-2 Turbo GGUF Q4_K_M (~7.5 GB, with the `ComfyUI-GGUF` custom node) |
| Style reference / realism | With LoRA | With LoRA (the same files as MAX/FLASH) |
| Thinking | unlimited | unlimited (thinking on, no budget) |
| Video | 832×480, up to 5 s | 832×480 cap, 3 s cap |
| ComfyUI | normal | `--lowvram` (weights stream layer by layer from RAM) |

Memory budget: measured on an RTX 4060 Laptop with the default Q5_K_M and 32K context, with the
vision projector (mmproj Q8_0) on the CPU (`--no-mmproj-offload`) — **~7249 / 8188 MiB** (708 MiB
free after loading; note the admission gate is deliberately small on LITE — only **300 MiB** of
model-file overhead is reserved, because the model already runs at the card's limit and llama.cpp
keeps the big embedding tensor on the CPU, so a wider gate would reject a working install),
generation **content-dependent ~40–79 tok/s** (MTP on;
acceptance ~0.93 (content-dependent 0.31–0.96)), prefill ~270 tok/s. On a 16 GB card LITE goes up to ~97 tok/s. **Before** the
projector was moved to the CPU (it ran on the GPU) the same setup used **~7885 / 8188 MiB** (~72 MiB
free) — llama.cpp's own automatic fit check marked that as "does not fully fit" and pushed the big
embedding tensor onto the CPU; since `--gpu-layers 99` is forced manually it loaded anyway. If you
want a more comfortable margin you can go back to Q4_K_M with the `liteModel` setting below
(~40 tok/s, ~1.7 GB of VRAM stays free).

**Thinking (unlimited) and the guaranteed answer.** The Qwen3.5 template ignores the
`--reasoning-effort` value; thinking stays ON (`--reasoning on`, `--reasoning-format deepseek`) and
**no thinking budget is imposed** — LITE thinks as long as needed. Because thinking tokens share the
`--predict` **output** budget, on a hard question thinking can consume all 8192 tokens and produce no
visible answer (measured: 8192 tokens ≈ 159.7 s, no answer). So the worker, when the visible answer is
**empty** or was cut off at the output cap (`finishReason='length'`) leaving a broken fragment,
retries with thinking **fully off** (`chat_template_kwargs.enable_thinking = $false` plus `/no_think`)
and **guarantees** the visible answer; the system prompt also steers the model to think enough and to
**always write its answer** (not to extend thinking endlessly). If you
want a more comfortable VRAM margin, write `liteModel:
"Huihui-Qwen3.5-9B-abliterated.i1-Q4_K_M.gguf"` into `%LOCALAPPDATA%\Programs\MamidLabs\settings\api.json`, download the
file manually from the same Hugging Face repository
(`mradermacher/Huihui-Qwen3.5-9B-abliterated-i1-GGUF`) and put it into the models folder — the
catalog only downloads Q5_K_M automatically. Q6_K (~7.4 GB) never fits on an 8 GB card (the file
itself exceeds the free VRAM).

**Expectation:** On Lite, chat is fluid. Image generation works but is noticeably slower than on the
16 GB profile because of `--lowvram`; the ~7.5 GB GGUF diffusion model + ~5.2 GB text encoder push a
16 GB RAM system hard. Video generation is borderline; memory may run short for long jobs.

### The Profiles menu

The **Profiles** menu at the top left scans the files on disk every time it opens and shows an
`installed` / `N files to download` status next to each profile. Clicking an installed profile
switches to it directly; a profile with missing files opens the **Profile manager**.

The manager window shows the profile's file list as `[installed]` / `[to download]` /
`[manual install]` and fetches them from Hugging Face with **Download missing**. The catalog lives in
`agent/profiles.json`; rows with an empty `repo` field have no download source defined and must be
installed manually. Currently all files of the three profiles are downloadable (LITE ~36 GB,
MAX/FLASH ~52 GB); every row is pinned to a Hugging Face commit and a byte size. The 27B language
model and mmproj of MAX/FLASH come from `windowsxp811203/Qwen3.8-27B-Abliterated-GGUF`, the Identity
Edit LoRA from `conradlocke/krea2-identity-edit`. No token is needed for public repositories; an
interrupted download resumes where it left off, an existing file is not downloaded again.
The selector shows the model you want without waiting for loading to finish. The model that is
ready is tracked separately; the old model's health response does not change the selection and a
pending message is not sent to the old model. With fast consecutive choices, the most recently
chosen model is loaded after the running management operation finishes. A load error is shown in
the status line.

For quick jobs (context summarization, image prompts), instead of switching the model, a
`reasoning_effort: low` is sent per request — the cost of a 15 GB reload is not paid.
The model names show as `max` and `flash`; for fast internal operations the `low` reasoning value is
sent to the template.

## Interface

- User messages on the right, model answers on the left, as bubbles.
- The thinking stream is shown live and collapses to a **single line** when done:
  `Thought (12.4 s) ▸`. Clicking it opens/closes the thinking steps.
  If you scroll up while the stream is running, your reading position and text selection are kept;
  when you go back to the bottom it follows the new text again.
- A counter always visible right to the left of the effort choice: `22.0 tok/s | 12.3K / 32.8K`.
  The speed and token counts come from the server's live stream measurements (K = 1000 tokens).
  Hovering shows the full input/output/context numbers. The counter does not disappear during agent
  tools or image/video generation; if the language model is not generating the speed is 0 and the
  last context count is kept. The status message is on a separate line.
- Toggles: **Internet**, **Agent** and, on the lower row, **Full access**. Image, video and document
  generation are done automatically with the relevant tool based on the intent of the message, not
  with separate chips.
- ＋ attaches an image, video or document. **Several** video and document files can be attached at
  once (drag and drop onto the window works too, multi-select is on in the file chooser); in the
  normal image understanding flow **only one image** is sent to mmproj.
  If the user selects several images with a text reading/OCR intent, a dedicated multi-image OCR
  tool is turned on; the images go through GPU PaddleOCR one by one and no multi-image payload is
  sent to mmproj. With a single file a large preview is shown; with several files a clickable name
  list is shown. Images are scaled to 1024×1024.
- When multi-image mode is turned on, a persistent notice appears in the chat; the notice is not a
  model answer and is not added to the model context. It also appears when the activation request
  is sent together with images. Opening the notice from history does not re-enable the mode.
- All attached documents stay readable for the whole chat; this is not limited to the last document.
  This permission does not extend to files next to the attached ones.
- Enter sends, Shift+Enter inserts a line break. While generating, Send → **Stop**, which only
  cancels the request; the model stays in memory.
- The **X** at the top right closes everything: llama-server, ComfyUI and background jobs.

### API connections

The chat providers are defined in a single table in `agent\api-providers.ps1`: OpenAI, Claude,
Gemini, DeepSeek and a custom (OpenAI-compatible) endpoint. The endpoint, the key environment variable (`OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`), the key page and the model list address
are read from there. The UI lists, the `ask_model` consultation tool and the model box use the same
table.

The **Custom (OpenAI-compatible)** provider connects local servers such as Ollama, LM Studio or vLLM,
or an in-house endpoint. The base URL is entered in the API connections window
(`{base}/chat/completions`, `{base}/models`); the key is optional, and without one no
`Authorization` header is sent.

In the "API connections" window, **Sign in to your account, get a key** opens the provider's key page
in the browser. **Verify and fetch models** tries the pasted key against the provider's model list
endpoint; 401/403 is shown as "rejected the key", and the chat models the account can access fill the
list. The key is written only into the request header and stored with DPAPI.

DeepSeek uses an OpenAI-compatible endpoint (`max_tokens` and the `reasoning_content` stream are
handled on the same path). Since its chat models do not accept images, `image_url` parts are reduced
to text; a "this provider cannot read images" note takes their place and the OCR result is used if
needed.

The Claude request is converted to the Messages API format. The `thinking` field is not sent: Opus
5.5 and Fable 5/5.1 reject the `disabled` value; without the field newer models think adaptively,
older ones run without thinking. Since a forced tool choice is also rejected while thinking is on,
`tool_choice` always goes out as `auto`. In the tool loop the signed `thinking` blocks, text and
`tool_use` blocks of Claude's answer are collected from the stream in order and sent back unchanged
in the next request. This record is kept only in the memory of that request; it does not go into the
chat file or to other providers. Since `max_tokens` also covers thinking, the lower bound on Claude
is 16000.

**Signing in with a subscription account** (OAuth) for OpenAI, Claude or Gemini is deliberately not
offered. These providers open consumer subscription sign-in only to their own apps; a third-party app
using the subscription session violates the terms of use and can get the account closed. The
supported path is an API key; the account connection window reduces this step to a single click.

## Context compaction

When the context exceeds 22,000 tokens, 65% of the old messages are summarized with low effort into
a single system message, and the last 35% is kept as is. Opening a new chat resets the counters.

Compaction runs **in the background right after the turn** (the `compactOnly` job), while the user
writes the next message, so a new message does not wait for the summary. If a message is sent or the
chat is cleared, the background job is cancelled; if needed, compaction at send time still works as a
fallback.

## First token latency (prefill)

Almost all of the time spent waiting for an answer is **prompt prefill**: llama-server reprocesses the
part of the prompt that is not in the cache. Therefore:

- The **start of the prompt must stay byte-identical across turns**. Per-turn lines added to the
  system later, such as "the user attached this document" or "an image was attached to the chat",
  do not live in the system prompt but **as a `[System note]` at the start of the last user
  message**. Since the system prompt is the first block of the prompt, even a one-character change
  there makes the whole 20K-token context get reprocessed.
- Requests go out with `cache_prompt: true`.
- After an image reading turn is done, the raw base64 data of the image is kept in the chat record
  and the UI preview; it is not sent again with the following normal messages. That way the mmproj
  prefill does not run again with every message. If the image needs to be examined again, attaching
  it again is enough; if it needs to be used as a source for image generation, the last image path
  kept by the app is enough.
- When the chat chips (Internet / Agent) are toggled, the tool definitions and the system prompt
  change; a full reprocess in that turn is unavoidable. Generation tools are turned on automatically
  based on the intent of the message.
- The local server is started with `--cache-reuse 256`; follow-up messages use the shared KV cache
  more widely. The turn in which an image was attached may still carry the mmproj encoding cost,
  but the following normal messages benefit from the cache of the shared text prefix.

Until the first token arrives the status bar shows a `Thinking… prefill 12 s` counter; the counter
field's tooltip (when hovering) shows the last request's first token time, how many tokens were
reprocessed and how many came from the cache.

### Measuring slowness

```powershell
%LOCALAPPDATA%\Programs\MamidLabs\runtime\pwsh\pwsh.exe -File %LOCALAPPDATA%\Programs\MamidLabs\scripts\diagnose-latency.ps1
```

It measures VRAM usage, the prefill time / tokens from cache across two consecutive turns, the
prefill and generation speed, and shows the reprocessing traces in the server log. Interpretation:

| Finding | Meaning |
|---|---|
| `from cache` ≈ 0 in turn 2 | The prompt prefix changes; the KV cache is not used |
| prefill < ~200 token/s | Layers are not on the GPU or VRAM overflows |
| generation < ~12 token/s | VRAM overflow (Windows shared memory) is very likely |
| VRAM 95%+ | Lower `--ctx-size` or reduce `--image-min-tokens` |
| generation ~27 tok/s on PRO | The 32K context did not fully fit and spilled ~400 MiB into shared memory; the auto 16K fallback prevents this (see the memory-budget section) |

If the answer stays empty (thinking alone may have used up the tokens reserved for the answer), a
single low-effort retry is made. The
earlier tool results and the working notes of the last turn are carried over into this retry; the
start and end of long notes are kept so that room for the answer is left in the context. If it stays
empty again, an explanation is shown instead of completing silently.

The connection to the local model does not use the system proxy settings. A connection error that
happens before the stream starts is retried once; after a partial answer has started the request is
not replayed automatically. If the stream is cut, the text received so far is kept and the real
error cause is shown. The cancel button also interrupts a pending network read.

For local requests the connection is re-established on every turn (`Connection: close`). The server
can close a keep-alive connection that sits idle while an agent tool runs (web search, OCR, document
reading); when the client reused that stale connection the stream failed mid-answer with "An existing
connection was forcibly closed by the remote host." (WSAECONNRESET). On loopback a fresh connection
costs next to nothing and the server-side KV cache (and its prefill savings) is unaffected. When a
usable result exists (finish_reason received, answer text present, or a tool call present) and the
connection drops, it is not reported as an error, so a completed answer does not get a spurious
"[Error]" line appended underneath. Only content-less, tool-less, incomplete drops are errors.

When the internet chip is on but the network drops (hotspot, DNS timeout), the web search, page
reading and reference image tools do not raise; they return valid error JSON so the agent loop
completes its answer without breaking. This behavior is verified by `tests/test_offline_mode.py`.

When a web tool fails this way, the model is stopped from "working around it" (retrying the same
result via `run_command` with ping/curl/wget/Invoke-WebRequest, or another page/search, or inventing
the result): the failure is detected by `Test-WebToolFailure` and a "[System note]" is appended to
the request copy — **without** writing to the persistent history — so the model answers clearly from
existing knowledge, or states that web access could not be established without inventing the result.
There is also a preventive rule in the system prompt. Because the small model (LITE) does not
always follow a prompt note, there are two **hard guards**: (1) once a web tool has failed in the
same request, **web tools are no longer offered** to the model in later rounds (this stops the search
loop); (2) `run_command` refuses **network-probe commands** (`ping`, `curl`, `wget`, `nslookup`,
`ipconfig`, …) without running them. These guards only trigger **after** a web failure; they do not
affect a user's unrelated, explicit command request. Contract tests: `tests/test_mamidlabs_contracts.py`.

Moreover, **`run_command` is offered to the model only when the user explicitly asks for a command
or a terminal** (`Test-CommandIntent`): patterns such as command, terminal, console, cmd,
powershell, bash, script/batch, .bat/.cmd/.ps1/.exe, run, execute, ipconfig, ping, curl, wget,
nslookup, netstat count as intent. On general questions the tool is not offered, so the model cannot
attempt to work around anything with commands. When the user explicitly asks, it still needs
permission (approval) as before.

## Image reading and OCR

When an image is attached, the user text is checked deterministically. "What is in the image?" and
similar understanding requests go directly to Qwen mmproj; PaddleOCR is not started. For requests
like "read the text in the image", "what does the picture say?" or "do OCR", the language model is
first moved to RAM, VRAM is handed to PaddleOCR and the plain image is scanned on the GPU only. If
the GPU engine produces no text the OCR result comes back empty; the image is kept and the mmproj
fallback is used. When OCR finishes the language model is reloaded with the same profile. If the OCR
result is usable, `image_url` is removed from the model request and the recognized text is sent
together with the user's question. Unless the user explicitly asks for an explanation/summary/
interpretation, when OCR succeeds the model is not asked again and only the extracted text is
returned. If OCR is not installed, fails or extracts no text, the image is kept and the mmproj
fallback is used.

If several images and an OCR intent are detected together, Qwen is given only the `read_images_ocr`
tool. When the tool is on, the status line says that multi-image sending is on; the tool reads the
images one by one on the GPU, joins the results with labels like `[Image 1]`, `[Image 2]` and reloads
the language model with the same profile at the end. A multi-image send without an OCR intent is
safely rejected; it does not enter the normal mmproj multi-image flow.

If the extracted text of a PDF opened with `read_document` is empty or shorter than 80 characters,
the existing CPU path of the same worker feeds the PDF pages to PaddleOCR. If the user explicitly
asks to transcribe the images/text inside the PDF, OCR runs mandatorily even if the PDF already has
text; the OCR text is passed to Qwen as the result of the document tool.

To install the OCR environment once, with PowerShell 7 and a current NVIDIA driver:

```powershell
%LOCALAPPDATA%\Programs\MamidLabs\runtime\pwsh\pwsh.exe -File %LOCALAPPDATA%\Programs\MamidLabs\scripts\install-ocr.ps1 -Warmup
```

The OCR result shows up in the chat bubble with its confidence, line and character counts; it is
saved in the chat history together with the model's answer.

**GPU compatibility.** `install-ocr.ps1` first installs the special GPU build Paddle publishes for
the RTX 50 (Blackwell/sm_120); the standard cu126 build can return empty OCR results on those cards.
The same build also produces text on an RTX 4060 Laptop (Ada Lovelace, sm_89), on both the GPU (plain
image, `ocr_image`) and the CPU (scanned PDF, `ocr_pdf`) paths (confidence 0.999). The special build
is not on an official release channel; if it cannot be downloaded, setup falls back to the official
stable channel (`paddlepaddle.org.cn`, cu126). `scripts\smoke-test.ps1` verifies both paths (can be
skipped with `-SkipOcr`).

## Image generation (Krea-2 Turbo)

Since two models do not fit into 16 GB of VRAM at the same time, the order is:

1. The language model writes the image prompt **in English** (the `generate_image` tool).
2. llama-server is stopped → VRAM is freed (the model file stays in the Windows file cache).
3. ComfyUI + Krea-2 Turbo are loaded, the image is generated (8 steps, cfg 1, euler/simple).
4. The models in ComfyUI are unloaded and the process is closed by default (it holds neither VRAM
   nor RAM while idle); with `comfyKeepAlive=true` (LITE only) the process is kept open (~130–370
   MiB VRAM idle). The language model is reloaded and the chat continues where it left off. If it
   was closed, the next image/video reopens ComfyUI (~27 s); on an error or cancel the process is
   closed again.

Measurement (RTX 5060 Ti 16 GB): generation **25–58 s**, end to end in the interface **60–95 s**
(including unloading and reloading the language model). LITE (RTX 4060 Laptop): 68–139 s end to end;
with the realism LoRA 180–210 s.

The realism LoRA is turned on only when your message explicitly asks for it ("realistic",
"photorealistic", "lifelike", "like a real photo", "true to life"…); negations like "not realistic"
keep it off. Since the LoRA slows generation down ~2x on an 8 GB card it is off by default. Aspect
ratios: 1:1 → 1024×1024, 16:9 → 1344×768, 9:16 → 768×1344, 4:3 → 1152×896, 3:4 → 896×1152.

### Three modes

The model chooses with the `mode` parameter; if you attached an image to the chat with ＋, the second
and third modes become available.

| mode | What it does | How it works |
|---|---|---|
| `new` | Generates from scratch | `EmptyLatentImage` |
| `regenerate` | **Keeps the composition** of the attached image, changes its style/detail | `VAEEncode` + denoise = `strength` (0.2–0.95, default 0.6) |
| `style_reference` | Carries the **style/character** of the attached image into a new scene | `krea2_style_reference` LoRA + `TextEncodeQwenImageEditPlus` (the image goes into Qwen3-VL as conditioning) |

The source image is scaled to ~1 MP and written under `outputs\input`, where ComfyUI reads it.

### Photo editing (Krea 2 Identity Edit)

On the local provider a fourth mode is added to the `generate_image` tool: `identity_edit`. When the
user wants to edit an attached photo ("edit the photo", "change their clothes", "change the
background", "add/remove this"), the model picks this mode and writes a one-sentence English editing
instruction into the `prompt` field.

| Part | Value |
|---|---|
| LoRA | `krea2_identity_edit_v1_2.safetensors` (strength 1.0) |
| Nodes | comfyui-krea2edit `86f886d`: `Krea2EditModelPatch` (source VAE latent, pixel path, `fit`, `ref_boost` 4) and `Krea2EditGroundedEncode` (the instruction goes into Qwen3-VL together with the source image, `grounding_px` 768) |
| Sampling | 10 steps, cfg 1, euler/simple (Turbo); negative: empty instruction + the same image |
| Resolution | The source's aspect ratio, ~1 MP, a multiple of 16 |
| Two images | `scene_from_previous=true`: the image from the previous turn is the scene, the last image is the person ("put this person in that scene") |

The UI version lives in `agent\workflows\krea2_identity_edit.json` (it can be opened in ComfyUI). The
code builds the same graph in API format and adds the `target_latent` connection the node pack
recommends. That connection encodes the source before sampling starts and prevents the model from
being split on cards with tight VRAM. On LITE the GGUF loader is used as is, and the LoRA is attached
on top of it. The realism LoRA is not added to the editing chain. If an API image provider is
selected, the same request goes to the OpenAI/Gemini image editing endpoint.

### Your own image/video model (file path)

"Image and video models → Choose model file (file path)…" **does not copy** the file by default. The
path is written to the `settings\api.json` → `externalMediaModels` list and
`settings\comfy-extra-models.yaml` is generated. ComfyUI reads this file with
`--extra-model-paths-config`. If the list changes, an idle ComfyUI is restarted; if the content is the
same the file is not touched. `.gguf` files show up under `unet`, the others under
`diffusion_models`.

## Video generation (Wan 2.2 TI2V-5B)

The same VRAM swap as image generation. Three modes, the `mode` parameter of the `generate_video`
tool:

| mode | What it does |
|---|---|
| `text` | Video from scratch |
| `image` | Starts motion from the attached image (image-to-video) |
| `video` | Continues from the **last frame** of the attached video |

Default: 832×480, 2 s (49 frames @ 24 fps), 20 steps, cfg 5, uni_pc/simple, shift 8.
`quality: high` goes up to 1280×704 but is much slower. The duration is limited to 1–5 s.

| Resolution · duration | 16 GB (RTX 5060 Ti) | 8 GB (RTX 4060 Laptop) |
|---|---|---|
| 832×480 · 2 s | ~132 s | ~5 min |
| 1280×704 · 3 s | ~339 s | limited to 480p |

**Important:** the video VAE overflows 16 GB of VRAM when it decodes all frames at once, so
`VAEDecodeTiled` (`tile_size=320, overlap=80, temporal_size=16, temporal_overlap=8`) is used. With a
plain `VAEDecode` ComfyUI crashes after sampling finishes. Smaller overlap values
(`tile=256/overlap=64, temporal_overlap=4`) leave blotches at the tile boundaries and a periodic
brightness jump; with the chosen values the VRAM peak is ~14.6 GB.

## Document tools

`read_document` reads PDF / Word / Excel / PowerPoint / **ZIP** / **RAR** (Desktop, Documents,
Downloads, `outputs`, the bound folder or a file attached with "Add document" from the ＋ menu). For
ZIP/RAR, without `entry` the archive listing comes back, with it the content of that file. RAR
reading works through 7-Zip (`%ProgramFiles%\7-Zip\7z.exe`; the setup wizard installs it with winget
if it is missing). `create_document` produces a PDF, Word document or presentation; the content is
written as markdown (headings, bullets, tables, bold). In PowerPoint every `# ` becomes a new slide.
The outputs go to the bound folder, otherwise under `outputs\documents`.

**Backups.** If `write_bound_file` or `create_document` is going to overwrite an existing file, the
previous version is automatically copied under `outputs\backups` as
`<file name>.<timestamp>.bak` (the write itself needs approval separately — see the permission system
below). This folder is not cleaned up by itself; if it grows over time it can be deleted manually.
The scope was deliberately kept narrow: the effects of commands run with `run_command` are out of
scope, and no "everything can be undone" promise is made.

## Running commands and the permission system

The **Full access** toggle at the bottom left of the chat box controls it:

- **Ask for permission** (default): when the agent is about to write a file, run a command or create a
  document, an approval window appears; the window shows the operation, the target file and the time
  left. If there is no answer within 10 minutes the operation is denied and the window closes by
  itself; X or Esc count as "No". For a denied operation the tool returns an explicit "NOTHING WAS
  DONE" result to the model, and a "Nothing was done" note independent of the model is added under
  the answer (on screen only; it does not enter the model's chat history, so the KV cache match is
  preserved).
- **Full access**: writing files, creating documents and `run_command` calls the user explicitly asked
  for are done without asking. A warning appears when turning it on; **the choice is saved and
  restored when the app is reopened as well**. Use the same button to turn it off. On a first
  installation, or if the saved value is invalid, the default is again **Ask for permission**. Turn on
  this persistent authority only on a computer you trust and accepting the risks of agent operations.

Read operations (reading files/documents/the web) need no permission, only permanent changes do.
Running a command asks for approval by default; if Agent is off, the `run_command` tool is not given
to the model.

## Past chats

After every completed turn the chat is saved as `%LOCALAPPDATA%\Programs\MamidLabs\chats\<id>.json` (the API messages +
the bubbles on screen, including thinking texts and image paths). The file content is encrypted with
DPAPI bound to the Windows account.
Multi-attachment lists and the access to documents the user attached are restored too; deleted files
are marked as "file not found" instead of disappearing from the list. The chat and the settings are
first written to a temporary file, and after a successful write the existing record is replaced
atomically. Save errors are reported to the user; the previous record is kept.
**File → Past chats…** opens a separate window; with a double click or *Open* a chat is restored as
it was, *Delete* deletes the selected one and *Delete all* deletes all chats in the list permanently
(both ask for confirmation). The main design does not change.

## Safety boundaries

- The web page and reference image tools reach only public HTTP(S) targets; loopback, private
  network, link-local, reserved addresses and untrusted redirects are rejected.
- ZIP/RAR and Office archives are read with a limited entry, uncompressed size and total size budget;
  ComfyUI outputs are accepted only inside their own output folder and with the expected extensions.
- There are size limits on attached images, videos and documents. These limits reduce the memory or
  disk consumption of malicious files; they do not limit model generation itself.

## Audio transcription (Whisper + audio events)

An audio file attached to the chat or a microphone recording is transcribed automatically. The
pipeline lives in the `agent/transcriber/` package and runs in `venv\asr`:

- **Speech:** `faster-whisper` **large-v3**. Silero VAD keeps non-speech spans away from Whisper;
  `condition_on_previous_text=False` plus `no_speech`/`log_prob`/`compression_ratio` thresholds and
  low-temperature fallback reduce hallucination.
- **Non-speech sounds:** AST (`MIT/ast-finetuned-audioset-10-10-0.4593`) tags events (footsteps,
  door, applause…). Speech labels are filtered. Default **on**; toggled by the microphone-menu
  **"Non-speech sounds"** checkbox (`api.json -> audioEvents`).
- **Speaker diarization:** pyannote (needs a HF token; skipped without one).
- **Output** is chronological and timestamp-free: `(footsteps)` / `Speaker: ...` lines.
- **Profiles (tier):** `config.py` now chooses the tier from the **selected language-model
  profile** (`select_profile_for`): **pro** (MAX/FLASH) → `float16`, models resident together;
  **lite** → `int8_float16`, models loaded sequentially and long recordings split into 30-minute
  chunks (`torch.cuda.empty_cache()` after each model). The old hardware names (`pc1`/`pc2`) and the
  UI names (`max`/`flash`) are aliases (`TIER_ALIASES`). Total VRAM is only a fallback metric when no
  profile name is given (CLI, no `--profile` → `select_profile`) (>= 14 GB → pro, otherwise lite).
- **CPU / GPU choice:** a GPU transcription requires unloading and restoring the local language
  model (swap). The device/swap decision is not made from the tier name but from the **measured real
  free VRAM + system RAM and the recording's duration** when the audio job starts (`choose_plan`): if
  Whisper + AST fit together with the language model in free VRAM, GPU (no swap); if not and the
  recording is shorter than the threshold, CPU (no swap); otherwise GPU + mandatory swap. Threshold
  **pro ~20 s**, **lite ~15 s** (`agent/transcriber/config.py` → `DEFAULT_CPU_MAX_SECONDS` /
  `Profile.cpu_max_seconds`). When the model is required but **not currently loaded** (an audio turn
  cancelled a model load, `Stop-ModelSwitchForAudio`) the plan **forces** the swap
  (`local_model_required` + the measured `local_model_loaded`): even though free VRAM looks large,
  the ASR models (~3.9 GB) take the language model's place and, if the model is not restored after
  the turn, the next load is rejected with "not enough free VRAM".
  `Invoke-TranscribeWithModelSwap` also re-measures at the end of the turn (`Test-LocalModelResident`)
  and restores the model.
- **Warm (persistent) worker:** the fixed cost repeated on every audio turn (Python start-up +
  `torch`/`transformers`/`faster-whisper` imports; measured ~10 s, ~25 s cold/disk) is paid once per
  session: `agent/whisper-tools.py --serve` runs as a single persistent process listening on
  127.0.0.1 (port/pid in `logs\asr-server.json`) and holds the models in-process;
  `{"action":"unload"}` drops the audio models (the process stays alive). Before the language model is
  reloaded into VRAM the worker is **stopped** (its remaining CUDA context ~140 MiB could eat the
  27B's margin) and restarted in the background after the turn; it exits itself after 600 s idle /
  when the app closes. If the worker is unreachable the transcript falls back to the one-shot
  `Invoke-PythonTool` path (behavior preserved).
- **HF cache (`HF_HOME`):** the audio models (whisper large-v3 + AST) live in the install root's HF
  cache (`tools\hf-home`) and are used **offline** at runtime; the app sets
  `$env:HF_HOME = <root>\tools\hf-home` in `mamidlabs-desktop.ps1` and `agent/mamidlabs-inference.ps1` passes
  `Get-HfHome` to both the one-shot `Invoke-PythonTool` (child process env) and the persistent
  `Start-AsrServerProcess`. Without it the worker falls back to the DEFAULT HF cache, AST is not
  there, and because `events.tag()` guards the load with `try/except` the transcript still completes
  but the **events are skipped** (empty list). So AST working depends on `HF_HOME` (the same path the
  installer uses).

## Verifying the fixes

In an environment with Python:

```sh
python -m pip install pytest
python -m pytest -q -rs
```

If `tests/conftest.py` finds an installed MamidLabs (`MAMIDLABS_ROOT`, otherwise `%LOCALAPPDATA%\Programs\MamidLabs`), it
uses the bundled `runtime\pwsh` even if there is no `pwsh` on PATH. If `pypdf`/`reportlab` are
missing, it also adds the `venv\app` packages (if the Python version is the same). In the
source repository only the OCR test that needs an installed tree is skipped. The install script
tests run with both PowerShell 7 and Windows PowerShell 5.1.

The behavior tests run the real PowerShell functions and event handlers; they use test objects
instead of the model, the GPU and the visual controls. The chat record and preference tests run on
temporary files. In an environment without PowerShell these tests are skipped; skipped tests must
not be counted as passing.

On Windows these checks are needed as well:

1. After an answer, turn on multi-image mode; the notice must not disappear when the timer runs.
   Send the same request with images, then save and reopen the chat.
2. Switch between MAX/FLASH and two custom GGUFs; the box must show the last choice right away, and a
   message sent during loading must start only once the chosen model is ready.
3. Turn on full access and restart the app; then turn it off and restart again. The button and the
   real tool approval behavior must match the saved choice.
4. Attach two documents from outside the allowed roots; both must be readable, and a sibling file
   that was not attached must be rejected. A past chat must keep the multi-attachment list and the
   document access.

These tests do not replace end-to-end verification of the real WinForms rendering, Windows DPAPI,
llama-server, GPU OCR, ComfyUI and paid API providers. For that:

```powershell
%LOCALAPPDATA%\Programs\MamidLabs\runtime\pwsh\pwsh.exe -File %LOCALAPPDATA%\Programs\MamidLabs\scripts\smoke-test.ps1 -Profile lite
```

`install-mamidlabs.ps1` only prepares the infrastructure; the models are downloaded separately from
the Profiles menu. This script runs after that step: it loads the real model, sends a real request,
produces a real `.docx`/image/video and proves that every component really works (can be skipped with
`-SkipChat`/`-SkipDocument`/`-SkipImage`/`-SkipVideo`). A non-zero exit code means at least one
component failed. On an RTX 5060 Ti 16 GB the image step takes 25–58 s and the video step ~132 s.
The output can be redirected to a file; the `llama-server` and ComfyUI that get started do not
inherit the caller's output pipe. The result is also written to `logs\smoke-test-last.json`.

## Known notes

- The `WinError 10022 ... _call_connection_lost` lines in the ComfyUI logs are harmless (Windows
  asyncio shutdown noise).
- A `llama-server` reload reads from disk the first time and from the RAM cache afterwards; the second
  load is noticeably faster.
- Avoid writing paths with non-ASCII characters into `.ps1` files (a PS 5.1 pitfall); this app runs
  with PowerShell 7, but the habit is kept.
- The install script runs with Windows PowerShell 5.1 on a clean machine. 5.1 strips double quotes
  from arguments going to native programs and turns stderr redirection into an error with the 'Stop'
  preference; the script passes Python code via stdin and makes native calls with
  `Invoke-NativeText`.
