# MamidLabs · Infrastructure guide for developers and AI agents

This file is the single source of truth for developers and AI agents working on MamidLabs. It
explains how the codebase starts, how the parts talk to each other, where things live and the known
pitfalls.

> **Mandatory rule:** When you add a feature, change a behavior or find a new pitfall, update this
> file **in the same commit**: fix the relevant section and add a line to the
> [Change log](#16-change-log) at the bottom. User-facing changes also go into `CHANGELOG.md`. If
> the code and this file disagree, the code is right; whoever notices the conflict fixes this file.

This is the **English edition**. Code identifiers (image/video modes, output folders, log and record
file names) are in English; the Turkish edition lives in `github.com/workaybarsh/MamidLabsTR` with
Turkish identifiers.

---

## 1. The product in one paragraph

MamidLabs is a fully local multimodal AI workspace for Windows. A single WinForms window offers
chat, web search, document reading/creation, OCR, image understanding, image generation, photo
editing, video generation and chart drawing. The language model runs on `llama.cpp` (`llama-server`), the
image/video models on `ComfyUI`, on the user's NVIDIA graphics card. OpenAI, Claude, Gemini,
DeepSeek and custom OpenAI-compatible endpoints are optional API providers.
Repository: `github.com/workaybarsh/MamidLabsENG`. Default install root:
`%LOCALAPPDATA%\Programs\MamidLabs`.

## 2. Repository map

| Path | Content |
|---|---|
| `mamidlabs-desktop.ps1` | Main app: WinForms UI, chat state, starting jobs, event loop |
| `launch-mamidlabs-desktop.vbs` | Target of the desktop shortcut; starts the app without a console |
| `agent/mamidlabs-inference.ps1` | Chat worker: model request, streaming, tools (tool calling), approval, context compaction |
| `agent/api-providers.ps1` | Provider table, request/response converters (OpenAI-compatible ↔ Claude Messages) |
| `agent/api-settings.ps1` | API connections window, key verification, DPAPI key storage |
| `agent/media-settings.ps1` | Image and video models window, "Choose model file (file path)" |
| `agent/hf-settings.ps1`, `agent/hf-hub.py` | Hugging Face token and download worker (size-verified) |
| `agent/web-server.ps1`, `agent/web-settings.ps1`, `scripts/web-serve.ps1` | Local web management (browser access): HTTP server, settings window, launcher. Localhost only by default, password protected; based on OpenCode's server model |
| `agent/imagegen-run.ps1` | Local image generation / editing worker (ComfyUI API) |
| `agent/videogen-run.ps1` | Local video generation worker (Wan 2.2) |
| `agent/chart-run.ps1` | Dependency-free chart generator (bundled pwsh + System.Drawing; chat tool `generate_chart`) |
| `agent/api-media-run.ps1`, `agent/api-media.py` | Image/video generation via API, profile download job |
| `agent/media-model-swap.ps1` | Unload the language model → start/reuse ComfyUI → reload (single owner) |
| `agent/media-paths.ps1` | External (not copied) model paths, `comfy-extra-models.yaml` |
| `agent/model-registry.ps1` | Model selector entries, profile aliases |
| `agent/profile-catalog.ps1`, `agent/profiles.json` | Profile file catalog, Profiles window, downloading missing files, update check |
| `agent/process-launch.ps1` | `Start-DetachedProcess`: start a child process without inheriting the stdout pipe |
| `agent/whisper-tools.py`, `agent/transcriber/` | Audio transcription (faster-whisper large-v3 + AST audio events + pyannote diarization); the worker `whisper-tools.py` calls the package |
| `agent/mamidlabs-agent-tools.py` | Web search (ddgs), page reading, bound-folder tools |
| `agent/office-tools.py` | Reading PDF/Word/Excel/PowerPoint/ZIP/RAR, creating PDF/DOCX/PPTX |
| `agent/ocr-tools.py` | PaddleOCR worker (GPU image, CPU PDF) |
| `agent/media-tools.py` | Video frame extraction etc. (PyAV) |
| `agent/workflows/krea2_identity_edit.json` | Photo editing workflow (ComfyUI UI format) |
| `scripts/install-mamidlabs.ps1` | The actual installation (staged, pinned versions) |
| `scripts/setup-run.ps1` | The wizard's back end: prerequisites → installation → models → summary |
| `scripts/uninstall-mamidlabs.ps1` | Full uninstall (Windows PowerShell 5.1, ASCII) |
| `scripts/manage-mamidlabs.ps1` | Start/stop/switch `llama-server` by profile, memory gate |
| `scripts/download-profile.ps1`, `profile-report.ps1`, `configure-models.ps1`, `install-ocr.ps1` | Profile download, hardware report, own model paths, OCR installation |
| `scripts/smoke-test.ps1` | End-to-end verification with the real models |
| `scripts/diagnose-latency.ps1` | VRAM, cache hit and speed diagnostics |
| `packaging/windows/mamidlabs.iss` | Inno Setup wizard; **the single source of the version** (`MyAppVersion`) |
| `requirements/app.txt`, `requirements/imagegen.txt`, `requirements/asr.txt` | Python packages (pinned versions) |
| `tests/` | pytest; also runs PowerShell functions with a real `pwsh` |
| `tests/ui-automation/` | Helpers that drive the app and the wizard with UI Automation/Win32 |
| `docs/` | `installation.md`, `technical-notes.md`, `media/` |
| `.github/workflows/` | `tests.yml` (pytest), `installer.yml` (Inno build and release publishing) |
| `AGENTS.md` | This guide. Not copied into the install root. |

## 3. Install root layout (`%LOCALAPPDATA%\Programs\MamidLabs`)

```
agent\ scripts\ assets\ requirements\   program files (copied again from the source on every installation)
mamidlabs-desktop.ps1 launch-mamidlabs-desktop.vbs README.md LICENSE EULA.txt
runtime\pwsh\pwsh.exe            bundled PowerShell 7.6.6 (the app runs ONLY with this)
llama.cpp\b10998\llama-server.exe CUDA 13.4 build
venv\app\                        the app's Python (tools, HF, documents)
venv\imagegen\                   ComfyUI + torch cu130 + PyAV
venv\asr\                        audio transcription (faster-whisper + transformers/AST + pyannote; torch cu130)
tools\hf-home\                   HF cache (whisper large-v3 + AST models live here)
venv\ocr\ venv\ocr-cpu\          PaddleOCR (optional, checked by default in the wizard; install-ocr.ps1)
ComfyUI\                         pinned commit; custom_nodes: ComfyUI-GGUF, comfyui-krea2edit
ComfyUI\models\{diffusion_models,unet,text_encoders,vae,loras}\
models\                          language models (.gguf + mmproj)
settings\api.json                all user settings (+ encrypted API keys)
settings\comfy-extra-models.yaml external model folders used without copying
settings\install-record.json     packages the wizard installed with winget (read by the uninstaller)
chats\                           chats (encrypted with DPAPI)
outputs\{images,videos,documents,backups,refs,thumbs,input,comfy}\
logs\                            mamidlabs-<profile>.std{out,err}.log, comfyui.*.log, audit.jsonl,
                                 setup.log, setup-summary.txt, setup-step.txt, smoke-test-last.json
tools\pip-cache\ tools\hf-home\  pip and Hugging Face caches (not written to the user profile)
tools\downloads\ tools\paddle-cache\ tools\backup\
setup-source\                    the source package extracted by the setup wizard
```

## 4. Launch chain

1. Desktop `MamidLabs.lnk` → `launch-mamidlabs-desktop.vbs` (finds the root from its own location).
2. VBS: `runtime\pwsh\pwsh.exe -NoProfile -ExecutionPolicy Bypass -STA -WindowStyle Hidden -File mamidlabs-desktop.ps1`.
3. `mamidlabs-desktop.ps1` first checks whether `Start-ThreadJob` exists. If it was opened with the wrong
   host (Windows PowerShell 5.1) it relaunches itself with the bundled pwsh.
4. It dot-sources the modules (`api-providers`, `model-registry`, `profile-catalog`, `api-settings`,
   `media-settings`, `hf-settings`, `media-paths`…), reads `settings\api.json` and builds the form.
5. `manage-mamidlabs.ps1 -Action switch -Profile <p>` is called for the selected profile; a timer polls
   `http://127.0.0.1:8080/health`. When it is ready the status becomes "Ready".
6. When the app closes (`FormClosing` → `Shutdown-All`) the chat is saved, and `llama-server` and an
   idle ComfyUI are closed too (VRAM is freed).

## 5. Processes and ports

| Process | Port | Owner | Note |
|---|---|---|---|
| `llama-server.exe` | 8080 | `scripts/manage-mamidlabs.ps1` | Mutex `Local\MamidLabsModelControl`; the profile is recognized via `--alias` |
| ComfyUI (`venv\imagegen\Scripts\python.exe ComfyUI\main.py`) | 8188 | `agent/media-model-swap.ps1` | Default: closed when the job ends (holds no VRAM/RAM while idle); `comfyKeepAlive=true` (LITE) keeps it open |
| Audio transcription worker (`venv\asr` python, `whisper-tools.py --serve`) | random, 127.0.0.1 only (`logs\asr-server.json`) | `agent/mamidlabs-inference.ps1` | **Warm/session:** the fixed import cost (Python + torch/transformers/faster-whisper, ~10 s) is paid once per session; before the language model is reloaded into VRAM the audio models are dropped and the worker is **stopped** (its remaining CUDA context ~140 MiB could eat the 27B's margin), and it restarts **in the background** after the turn; it exits itself after 600 s idle or when the app closes (no orphan process/VRAM/RAM leak) |
| App | — | `pwsh.exe … mamidlabs-desktop.ps1` | Workers are `Start-ThreadJob` runspaces in the same process |

**Profiles** (`manage-mamidlabs.ps1`; the aliases must match `model-registry.ps1` exactly):

| Profile | Alias | Model | Important flags |
|---|---|---|---|
| MAX | `qwen3.8-27b-max` | Qwen3.8 27B IQ4_XS + mmproj F16 | `--no-mmproj-offload` (projector on the CPU), ctx 32768, predict 8192, `--reasoning-effort xhigh`, q4_0 KV, batch 512/256, `--spec-type draft-mtp` |
| FLASH | `qwen3.8-27b-flash` | same files | predict 8192, `--reasoning-effort low`, `--spec-type draft-mtp` |
| LITE | `qwen3.5-9b-lite` | Huihui-Qwen3.5 9B Q5_K_M + mmproj Q8_0 | ctx 32768, predict **8192** (thinking tokens share the output budget, so 3072 was too small), thinking on and **unlimited** (`--reasoning-budget` not passed), batch 256/128, projector on the CPU (`--no-mmproj-offload`), `--spec-type draft-mtp` (added on download). The system prompt tells it to think enough and to **ALWAYS WRITE ITS ANSWER** (it does not encourage extending thinking endlessly); even if thinking consumes the budget on a hard question, the visible answer is **guaranteed by the thinking-off recovery** (`enable_thinking=$false`) |

**MTP (Multi-Token Prediction).** The Qwen3.8 27B GGUF carries a built-in MTP/nextn draft head
(`qwen35.nextn_predict_layers = 1`, `blk.64.nextn.*`); the LITE 9B GGUF loses that head in the
conversion, so `agent/mtp_inject.py` adds it back on download (source: the `huihui-ai/
Huihui-Qwen3.5-9B-abliterated` MTP tensors; only `blk.<N>.nextn.*` plus a `w+1` on the main model's
norms). So `--spec-type draft-mtp` needs **no separate draft model** and is lossless (accepted tokens
are verified against the target model). Measured: acceptance ~0.93 (content-dependent 0.31–0.96) on LITE, ~2x speed (36 → ~75
tok/s); the model card reports 0.35–0.53 on Q4_K_M for the 27B. The flag is added only if the GGUF
really carries MTP layers (`Test-GgufHasMtp`); passing it to one that does not makes llama.cpp fail
with "model doesn't contain MTP layers". Disable with `settings/api.json` → `"mtp": false`. When
running without MTP the server logs `unused tensor blk.64.*` warnings (expected).

Since MAX and FLASH use the same files, the setup wizard and the Profiles window show them as a
single "Qwen3.8 27B" entry (`Get-ProfileCatalogForDisplay`); switching happens in the model bar.
Lookups by id (`Get-ProfileEntry`, `Get-ProfileState`, `Test-LocalProfileChatFilesPresent`) must use
the full catalog (`Get-ProfileCatalog`).

**VRAM check** (`Test-MemoryBudget`, context-aware via `Get-ModelOverheadMiB`): required = model
file + **1200 MiB at a 32K context / 800 MiB at 16K** (MAX/FLASH; 300 MiB on LITE, unchanged);
the mmproj runs on the CPU in all profiles so it does not count toward the VRAM
budget. **MAX/FLASH context selection:** 32K is tried first and, when its estimated footprint does
not fit the free VRAM, it **falls back to 16K** (same model/weights, half the KV cache); the chosen
value feeds both the gate and `--ctx-size`, and the UI reads the real value back from the server
(`/props` → `default_generation_settings.n_ctx`) so the compaction threshold matches. **ComfyUI lifecycle (default: runs only during generation):** ComfyUI runs **only
during image/video generation**; it is closed when the job ends and before the language model loads,
so it holds **neither VRAM nor RAM while idle** (an idle ComfyUI kept ~300 MiB of CUDA context plus
RAM; `comfyKeepAlive` default `false`). It reopens on demand (~27 s).
- **Local mode (MAX/FLASH):** the 27B sits at the card's limit on 16 GB, so ComfyUI is **always**
  closed before loading (otherwise the MTP draft context moves to the CPU and chat drops
  intermittently to ~6-7 tok/s; measured: the "post-media switch" step once hit ratio 0.18).
- **Local mode (LITE):** the 9B can coexist with ComfyUI (measured ratio 1.04); to keep it open
  between media jobs set `settings/api.json` -> `comfyKeepAlive=true` (it then holds ~300 MiB VRAM +
  RAM while idle; recommended only on a 16 GB card — the LITE target is 8 GB VRAM / 16 GB RAM).
- **API mode:** the local model is not running (GPU free), so after media the ComfyUI process is
  left open (no VRAM conflict, no reopen).
A **leaked/duplicate** ComfyUI instance is cleaned in every profile first: ComfyUI is a process
**pair** (a venv launcher +
its base-python child), so process **count** cannot decide "leak" — a single healthy instance is
already 2 processes. Instances are counted by **roots** (a matching process whose parent is not
itself a match; `Get-ComfyInstanceRoots`); `Stop-ComfyLeaks` closes only extra roots and **keeps the
instance listening on :8188** (`Test-ComfyUnderRoot`). Measured: two ComfyUI instances -> 27B spilled
to **0.89 tok/s**. If the budget fails ComfyUI is closed and measured again (safety net). External
cleanup: `manage-mamidlabs.ps1` `-Action closecomfy`. Reopening ComfyUI costs ~27 s per media job
(measured: cold 55 s, warm 28 s).
Measured (llama.cpp b10998, RTX 5060 Ti 16 GB): the 27B file is 14,600 MiB but its real GPU
footprint is **~15.32 GiB at a 16K context** and **~15.77 GiB at 32K** (llama.cpp's own report:
model buffer 13945 MiB, KV 288/576 MiB, recurrent-state buffer 598.5 MiB, compute ~112 MiB). A
normal Windows desktop only leaves ~15.50 GiB free, so 32K used to **spill ~400 MiB into shared
memory and drop chat from ~40 to ~27 tok/s**. The margin is therefore context-aware (800/1200 MiB
= the measured footprint); the gate **tries 32K and falls back to 16K** so the model only loads
when it fully fits and then **uses all free VRAM**. `--gpu-layers 99` is kept (puts the whole
model in VRAM; ~55 tok/s when fully fitting); llama.cpp's `--fit` auto-placement was tested and
proved more **conservative** (did not use all VRAM, ~23 tok/s), so it is not used. The tolerance is
profile-based:
**LITE 1024 MiB** (9B MTP model's small overflow on an 8 GB card; llama.cpp keeps the big embedding
tensor on the CPU), **MAX/FLASH 0** (27B runs at the card's limit; even a small overflow is a
disaster). Measured: 27B ~40–58 tok/s when it fits fully (16K fallback ~40, 32K ~44–58), LITE
~97 tok/s.
Before the model loads, leftover app-owned **GPU workers** (an idle persistent audio worker or the
OCR worker) are also closed automatically (`Stop-OrphanWorkers`), so the VRAM cleanup is automatic
and only touches MamidLabs processes under the install root.

## 6. Flow of a chat request

1. The user sends → `Begin-Generation` (`mamidlabs-desktop.ps1`).
2. `Build-JobMessages`: fixed system prompt (`Get-SystemPrompt`) + history. Image data from older
   turns is dropped and a note is put in its place. Per-turn hints (`Get-TurnHints`) are added **to
   the start of the last user message** as a `[System note]`, not to the system prompt, so
   llama-server's KV cache prefix is not broken.
3. `Start-ThreadJob -FilePath agent\mamidlabs-inference.ps1 -ArgumentList $jobData, $sharedRequest`.
   `$jobData.root` carries the root **explicitly** (see pitfall 2).
4. The worker produces `[pscustomobject]@{kind; text; usage; data}` events with `Emit`. The UI timer
   reads them. Kinds: `thinking`, `answer`, `answer-rewrite`, `status`, `metrics`, `tool`,
   `artifact`, `chart`, `ocr`, `imagegen`, `videogen`, `compact`, `denied`, `cancelled`, `error`,
   `done`.
5. **Tools** (`mamidlabs-inference.ps1`): `search_web`, `fetch_page`, `read_document`,
   `read_images_ocr`, `create_document`, `generate_image`, `generate_video`, `generate_chart`,
   `find_reference_image`, `ask_model`, `list_bound_files`, `read_bound_file`, `write_bound_file`,
   `run_command`. Tools are defined based on intent and the chips (Internet, Agent). Generation
   tools (image/video/document/chart) run only when the user explicitly asks: like
   `generate_image`, all are guarded by a **call-time intent check** (`Test-*-RequestIntent`); when
   a current-information turn has no web access the model text is **not used** and `Get-FinalAnswer`
   shows an honest message, and `read_document` is offered only with a document context.
6. **Approval:** write/run tools write `$Shared.AskTool/AskDetail/AskId` via `Request-Approval`.
   The UI opens the "Permission required — <tool>" window (10 min timeout → denial). If the "Full
   access" chip is on, nothing is asked. After a denial the UI adds a model-independent "Nothing was
   done" note. A tool denied (or timed out) in a request is not asked again in the same request;
   it is denied directly. Only in the request copy is a "[System note] … DENIED" user note added to
   the next model call; small models can skip an instruction in a tool result, so the note matters.
7. **The `done` event** carries, together with the final text, the `assistant.tool_calls` and `tool`
   messages added in that turn. The UI appends them permanently to `$chatMessages`. Otherwise the
   client's prefix differs from the server's in the next turn and the context is reprocessed from
   scratch (pitfall 5).
8. When the context threshold is exceeded a separate compaction job (`$script:compactJob`) writes a
   summary.

## 7. Image, photo editing and video

- When the model calls `generate_image` the worker emits an `imagegen` event. The UI starts
  `agent\imagegen-run.ps1` with `Start-ImageGeneration`. The language model only writes the prompt.
- `media-model-swap.ps1`: `Stop-LocalModelForMedia` → `Start-OrReuseComfy` → generation → reload the
  language model with `restoreProfile`. On LITE ComfyUI uses `--lowvram` and Krea-2 GGUF.
  - After a job `Release-Comfy` unloads the models via `/free`; **even when the torch counter resets
    it verifies the real free VRAM (`nvidia-smi`)** and closes ComfyUI when it is not enough for the
    model to reload (MAX/FLASH 14000 MiB, LITE 6000 MiB). Otherwise the CUDA context holds VRAM while
    the language model starts, it spills into shared memory and chat drops to ~6 tok/s.
- **Modes** (`generate_image.mode`):
  - `new`: generates from text. Krea-2 Turbo, 8 steps, cfg 1.
  - `regenerate` / `style_reference`: start from the attached image.
  - `identity_edit`: works with the Krea 2 Identity Edit v1.2 LoRA
    (`krea2_identity_edit_v1_2.safetensors`) and the `comfyui-krea2edit` nodes; the person's face is
    preserved. **MAX/FLASH (16 GB+) only**; not offered on LITE (see section 13), where photo
    editing requests are routed to `regenerate` (`agent/mamidlabs-inference.ps1`
    `$job.imageProfileLite`; no LoRA in `agent/profiles.json` for LITE).
  - `identity_edit` + `scene_from_previous=true`: the previous image is the scene, the last image the
    person.
- In the editing prompt the model turns the user's request into a one-sentence English instruction.
  The tool description has **no concrete example**; examples steer the model on vague requests.
- The realism LoRA is loaded only if the request carries a realism intent. ComfyUI's Krea2 LoRA key
  prefix problem is fixed at runtime with `Repair-LoraKeyPrefix`.
- Video: `videogen-run.ps1`, Wan 2.2 TI2V 5B; modes `text`, `image`, `video` (continue from the last
  frame); quality `normal` / `high`. The VAE is decoded in tiles: `VAEDecodeTiled` `tile_size=320,
  overlap=80, temporal_size=16, temporal_overlap=8`. Smaller values leave tile boundary marks.
- API media: if the image/video provider is an API, `api-media-run.ps1` → `api-media.py`.
- External model files are used without copying: `settings\comfy-extra-models.yaml` → ComfyUI
  `--extra-model-paths-config`.

## 8. API providers

- A single table: `Get-ChatProviders` (`agent/api-providers.ps1`): `openai`, `claude`, `gemini`,
  `deepseek`, `custom`. Every row has the endpoint, the model list URL, the key page, the environment
  variable and a `vision` field. A new provider = one row in the table (plus a converter if it is not
  OpenAI-compatible).
- **`custom` ("Custom (OpenAI-compatible)"):** the user's own OpenAI-compatible endpoint (Ollama,
  LM Studio, vLLM, an in-house proxy etc.). The base URL is read from the API connections window
  (`settings\api.json` → `providers.custom.baseUrl`, `Get-CustomBaseUrl`); `{base}/chat/completions`
  and `{base}/models` are derived. **The key is optional**; without one no `Authorization` header is
  sent. That is why `custom` is exempt from the key-required checks (three places in
  `mamidlabs-desktop.ps1`, `Invoke-AskModel` and the main request loop in `mamidlabs-inference.ps1`;
  `-and $providerId -ne 'custom'`).
- `Convert-ProviderRequest` / `Convert-ProviderChunk`: convert the OpenAI chat format to the Claude
  Messages API and back. DeepSeek does not accept images; `image_url` parts are reduced to a text note.
- **Claude:**
  - The `thinking` field is **not sent**; Opus 5.5 and Fable 5/5.1 reject the value `disabled` with 400.
  - A forced `tool_choice` always goes out as `auto`.
  - The minimum `max_tokens` is 16000.
  - In the tool loop, signed `thinking` blocks are collected with `Add-ClaudeReplayChunk` and sent
    back in order via `$script:ClaudeReplay[first tool_use id]`.
  - This record is kept in memory only; it does not go to disk or to other providers.
- Keys are stored in `settings\api.json` → `providers.<id>.key` as `ConvertTo-SecureString` (DPAPI,
  bound to the Windows account). The `ask_model` tool lets the local model ask another provider.
- API flows are tested with fake responses; an end-to-end test with a real API key has not been done
  yet.

## 9. Profiles, catalog and downloads

- `agent/profiles.json`: the file list for each profile. Each row has `target` (relative to the root),
  `repo`, `revision` (pinned commit), `filename` and `bytes`. A row with an empty `repo` counts as
  "install manually"; there are currently no such rows.
- 27B language model and mmproj: `windowsxp811203/Qwen3.8-27B-Abliterated-GGUF@efb07ba`.
  Identity Edit: `conradlocke/krea2-identity-edit@89e9e7a`.
- Download sizes: LITE ~36 GB, Qwen3.8 27B (MAX/FLASH) ~52 GB.
- `hf-hub.py` downloads and verifies the size against `bytes`; an incomplete file resumes where it
  stopped. `HF_HOME` is under the root.
- The profile status only checks whether the file exists; it does not compare sizes.
- **Deleting a profile** (Profile manager → "Delete profile"): only files UNIQUE to that profile
  are removed (`Get-ProfileUniqueTargets`); files shared with other profiles are kept. LITE's
  media files other than the Krea-2 GGUF are shared with MAX/FLASH. MAX and FLASH use the same
  file set, so they are treated as aliases; deleting one removes both. If the active profile is
  deleted, `Stop-LocalMamidLabs` runs first, then `Update-ProviderSelection` refreshes the list.
  Measured: LITE ~14 GB, MAX/FLASH ~29 GB.

## 10. Installation, wizard, updates and uninstall

- `install-mamidlabs.ps1 -Profile <p> [-Root] [-Stage a,b] [-SkipMedia] [-Force] [-NoUninstallEntry]
  [-AppVersion]`.
  Stages in order: `preflight`, `layout` (program files, shortcut, Windows uninstall entry),
  `runtime`, `llama`, `python`, `asr` (audio-transcription environment, `venv\asr`; skipped with
  `-SkipAudio`), `comfyui` (brings the pinned commits, backs up local changes to
  `tools\backup\*.patch`), `verify`. If the app is open, `layout` stops because the files are
  locked. Package sizes are verified. The `-Root` default is `%LOCALAPPDATA%\Programs\MamidLabs`.
  `requirements\` is created and copied into the root too (the `Read-Requirements` step reads from
  `-Source`, but the root keeps a copy like the wizard does). `layout` prunes the owned program
  folders (`agent\`, `scripts\`, `assets\`, `requirements\`) before copying and applies the same
  excludes as the `.iss` (`agent\__pycache__\*`, `*.pyc`); `assets\installer\*` is **included** so the
  installed program files stay byte-identical to the repo. Removed/renamed
  files do not linger. Top-level files get the same protection for the renamed old names: `Remove-LegacyTopFiles` removes `qwen-desktop.ps1` / `launch-qwen-desktop.vbs` from the root and `setup-source`, and the .iss `[InstallDelete]`/`[UninstallDelete]` and the uninstaller `$programEntries` target the same names. User data (`settings\`, `chats\`, `models\`, `logs\`, `outputs\`, `tools\`,
  venvs, `ComfyUI`) is never touched. Its script uninstall entry carries `DisplayVersion` (from
  `-AppVersion` or `MyAppVersion` in the `.iss` under `-Source`) so the in-app update check
  can read a script-installed copy; only a strict `x.y.z` `MyAppVersion` is accepted (the updater
  parses `DisplayVersion` as a `[version]`, so a non-semver value is ignored and no
  `DisplayVersion` is written at all). If an Inno `{AppId}_is1` key already points at the same root
  (wizard install), the script key is not written again (no duplicate entry).
  In a wizard-installed root, `layout` also refreshes `setup-source` if present (the source package
  the wizard extracts, used for cancel/rollback and reinstall); it does not create it for a
  script-based install. The `$sameRoot` guard is equality-only, so the wizard's nested
  `-Source <root>\setup-source` still copies the program files into the root; the `setup-source`
  refresh itself is skipped when the source IS `setup-source` (a self-copy would empty the package).
- Pinned versions: PowerShell 7.6.6, llama.cpp b10998 (CUDA 13.4), ComfyUI `b0f4b7b`, ComfyUI-GGUF
  `6ea2651`, comfyui-krea2edit `86f886d`, torch cu130.
- Disk thresholds (`profile-report.ps1` and `install-mamidlabs.ps1`, identical): LITE 58 GB,
  FLASH/MAX 75 GB of free space; below that the profile is marked "borderline".
- The "Prerequisites" step of `setup-run.ps1` installs, besides Python and Git, **7-Zip**
  (`7zip.7zip`; RAR extraction, `office-tools.py` looks for `%ProgramFiles%\7-Zip\7z.exe`) and the
  **Visual C++ Redistributable 2015-2022 x64** (`Microsoft.VCRedist.2015+.x64`; checks for
  `vcruntime140.dll`). If these two stay missing, setup does not stop; a note is recorded. Installed
  packages are written to `settings\install-record.json`.
- Wizard (`mamidlabs.iss`) pages: welcome → license → folder (default `{autopf}\MamidLabs`,
  `PrivilegesRequired=lowest`) → profile report → profile → (your own models) → (API) → (tasks) →
  ready → installing → summary → finish.
  - After "Install", `setup-run.ps1 -FromWizard` runs hidden (`SW_HIDE`), started with `ewNoWait`,
    and the wizard pumps its own message queue with `PumpMessages` (pitfall 20).
  - Progress: at every step `setup-run.ps1` writes 3 lines to `logs\setup-step.txt` (step number /
    total / step name); the wizard reads it every 200 ms in `UpdateInstallProgress` and updates
    `FilenameLabel` and the progress bar.
  - Completion: `setup-run.ps1` writes `logs\setup.pid` at the start and `logs\setup-done.flag` at
    the end (on success/failure/exception, including a `trap`; with retries). The wizard waits for
    the flag and every ~2 s also checks with `ProcessIsRunning` whether the process is still alive.
  - Cancel: while installing, `CustomCancelButton` is shown instead of Inno's Cancel button
    (pitfall 20). If confirmed: `taskkill /PID <pid> /T /F` → `uninstall-mamidlabs.ps1 -Yes
    -FromWizard` → `ExitProcess(1)`.
  - The summary is shown from `logs\setup-summary.txt`; all output is in `logs\setup.log`.
- **Versioning and releases:** the single source of the version is `MyAppVersion` in
  `packaging/windows/mamidlabs.iss` (Semantic Versioning, `MAJOR.MINOR.PATCH`). `installer.yml`
  builds the wizard and uploads an artifact on every push/PR. When a `v*` tag is pushed it checks
  that the tag matches the `.iss` version, uses the `## [x.y.z]` section of `CHANGELOG.md` as the
  release notes and uploads `MamidLabs_Setup.exe` to a GitHub Release. New release: bump the `.iss`
  version, add a CHANGELOG section, push the `v<version>` tag.
- **In-app updates:** the "Check for updates" button next to "Switch to this profile" in the Profiles
  window (`agent/profile-catalog.ps1` `Test-MamidLabsUpdateAvailable`,
  `Get-InstalledMamidLabsVersion`). It compares GitHub's `releases/latest` version with the local
  version. The local version is read primarily from Inno's uninstall key
  (`{AppId}_is1\DisplayVersion`); if that key is absent (a script-based install) it falls back to the
  script's own entry, matched by `InstallLocation`/`DisplayName`. Three states: idle → check → if an
  update exists "Update now" (downloads the setup file to `%TEMP%`, verifies its size, starts it and
  **closes the main window**, pitfall 21); otherwise "Up to date" or the error text, resetting after
  3 s. The result is also written to the window's status line. If the local version cannot be read,
  an update is assumed. The same wizard, the same `AppId` and `UsePreviousAppDir` update the existing
  folder; `settings\`, `chats\` and the models are kept.
- Desktop shortcut: if `MamidLabs.lnk` points to another root it is left alone; this installation
  gets `MamidLabs (<folder>).lnk`.
- **Uninstall:** Settings → Apps → MamidLabs, or
  `powershell -File <root>\scripts\uninstall-mamidlabs.ps1 [-DryRun] [-KeepUserData] [-KeepPrerequisites] [-PurgeSharedCaches] [-Yes]`.
  - First stops the processes running from the root.
  - Deletes the known entries in the root (including the `EULA.txt` and `requirements\` that
    `layout` puts there), the `MamidLabs*.lnk` shortcuts pointing to this root, the
    Uninstall entries belonging to this root and the winget packages in the record.
  - Does not touch files outside the root. Refuses to run in system and user folders.
  - The wizard's uninstaller calls the script with `-Yes -FromWizard`.

## 11. Settings and user data

- `settings\api.json` (the app keeps it in memory and writes it on change and on exit; **manual edits
  while the app is open get overwritten**):
  - Model selection: `selected`, `selectedModel`, `selectedLocalModel`, `localProfile`.
  - Providers: `providers.<id>.{key, model, models, contextLimit, baseUrl}`.
  - Your own models: `localModels`, `imageLocalModel`, `videoLocalModel`, `imageProvider`,
    `videoProvider`, `externalMediaModels`.
  - UI: `uiPreferences.{internet, agent, accessMode}`, `firstRunApiSettings`.
  - LITE settings: `liteModel`, `liteContext`.
  - Media: `comfyKeepAlive` (default `false`; ComfyUI runs only during media generation and holds no VRAM/RAM while idle; set `true` on LITE to keep it open between media jobs, see section 5).
  - Audio: `audioEvents` (default `true`; the microphone-menu **"Non-speech sounds"** checkbox. When checked, AST non-speech events are added to the transcript; when unchecked only the Whisper speech text is written).
- Chats are encrypted under `chats\` with the `MAMIDLABS-CHAT-DPAPI-V2` prefix.
- **"Delete all" in the Past chats window never deletes the chat that is currently OPEN:** the
  file of the chat on screen is removed from the delete set (`Get-DeletableChatPaths`), so the open
  chat stays in history.
- `logs\audit.jsonl`: a record of tool calls (not content; path/command identity).

## 12. Tests

```powershell
python -m pip install pytest -r requirements\app.txt
python -m pytest -q          # ~160 s (480 collected, 476 passed, 4 skipped); no extra setup needed (tests/conftest.py)
%LOCALAPPDATA%\Programs\MamidLabs\runtime\pwsh\pwsh.exe -NoProfile -ExecutionPolicy Bypass -File %LOCALAPPDATA%\Programs\MamidLabs\scripts\smoke-test.ps1 -Profile flash -SkipVideo
```

- **`conftest.py`:** if `pwsh` is not on PATH it adds the bundled pwsh of an installed MamidLabs
  (`MAMIDLABS_ROOT` or the default root) to PATH and adds the `venv\app` packages to `sys.path`.
  Many tests extract PowerShell functions via AST and run them in a real pwsh. Without PowerShell 7
  these tests are skipped; a skipped test does not count as a pass.
- `test_uninstall.py` really deletes in a fake root; it uses Windows PowerShell 5.1.
- **Smoke test steps:** `chat`, `document`, `chart` (`-SkipChart`), `offline` (the web tool returns error JSON without
  crashing while offline), `network_switch` (no turn errors while the network goes online/offline),
  `history_resume` (save chat → reload → continue; no errors), `mtp` (when the server was started with
  `draft-mtp`, the `/completion` response's `timings.draft_n_accepted` must be > 0; 0 means the head is
  ineffective; skippable with `-SkipMtp`), `ocr`, `image`, `edit`,
  `post_media_switch` (the speed
  ratio after an image must be ≥ 0.7), `video`, `ui_chat` (drives the real window like a user:
  types into the prompt box + clicks Send; a response must arrive), `ui` (launches the real desktop
  app; the window +
  model must be ready, one turn with the real worker the UI uses must answer with no
  "connection dropped"/watchdog text, and MTP must work; skippable with `-SkipUi`). `ui` runs last
  because the app stops the server on exit. Audio input (transcription) is verified through the
  real worker's `transcript` event and `tests/test_transcriber.py`, not via `/completion`. The engine
  is **faster-whisper large-v3** + AST audio events; the `agent/whisper-tools.py` worker calls the
  `agent/transcriber/` package and runs in `venv\asr` (torch cu130 + faster-whisper + transformers +
  pyannote). The output is chronological and timestamp-free: `(footsteps)` / `Speaker: ...` lines.
- **CI:** `tests.yml` (windows-latest, Python 3.12) on every push/PR; `installer.yml` when
  package-related paths change. Merged into `master` via PR.

## 13. Hardware facts (measured)

| | 16 GB (RTX 5060 Ti) FLASH | 8 GB (RTX 4060 Laptop) LITE |
|---|---|---|
| Chat | ~40–58 tok/s (MTP on; 32K ~44–58, 16K fallback ~40) | ~97 tok/s (on 16 GB with MTP) |
| Image (`new`), end to end | 60–95 s | 68–139 s |
| Regenerate from an image (`regenerate`) | not measured | ~110–140 s |
| Photo editing (`identity_edit`) | 74–160 s (one/two images) | **not supported** |
| Post-media switch | ratio 1.01, 319 MiB free VRAM | ratio 0.99–1, 310–369 MiB free VRAM |

Desktop apps hold VRAM: NVIDIA Overlay ~1.45 GB and dwm ~0.8 GB were measured. For per-process VRAM
use `Get-Counter '\GPU Process Memory(*)\Dedicated Usage'`; `nvidia-smi` does not show per-process
memory under WDDM.

**Why there is no `identity_edit` on 8 GB:** in plain generation the text/image encoder (Qwen3-VL 4B,
~5 GB) runs first and is evicted from VRAM, then the diffusion model (GGUF Q4_K_M on LITE, ~7.5 GB)
is loaded alone. `Krea2EditGroundedEncode` gives the image to both in the same graph, so ComfyUI tries
to keep both in VRAM at once: 5 + 7.5 GB > 8 GB, `--lowvram` moves ~2 GB to the CPU and the job takes
641–884 s (5–8 times the 16 GB time). A small LoRA (r64) does not fix it either (641 s). So on LITE
the mode is never offered, `imagegen-run.ps1` rejects it as a second line of defense and the catalog
has no LoRA.

**Audio transcription and CUDA context (measured):** transcription (faster-whisper large-v3 + AST)
uses ~3.9 GB on the GPU; it does **not** fit alongside the 27B (~15.3–15.8 GiB) on the same 16 GB card,
so the language model is closed before transcription (mandatory swap). After a load + unload the
CUDA context left in the process was measured at **~140 MiB** (8860 → 8720 MiB free with a game
running). That is why the **warm audio worker** is STOPPED before the language model is reloaded and
restarted in the background after the turn: if the worker has not transcribed yet it has no CUDA
context at all, so it holds no VRAM while idle and does not eat the 27B's thin margin (see §5).

## 14. Critical pitfalls

1. **The app only runs with the bundled PowerShell 7.** `Start-ThreadJob` does not exist in 5.1. If
   it is opened with the wrong host it silently stays at "Could not start the request";
   `mamidlabs-desktop.ps1` therefore relaunches itself.
2. **`$PSScriptRoot` is empty inside `Start-ThreadJob -FilePath`.** Workers get the root through
   `$JobData.root` (`Get-InstallRoot`). Do not hardcode paths when adding a new path to a worker;
   `test_install_root_portability.py` catches this.
3. **Windows PowerShell 5.1 reads a `.ps1` without a BOM as ANSI.** Save 5.1 scripts with non-ASCII
   characters as UTF-8 **with BOM**, or keep them ASCII. `uninstall-mamidlabs.ps1` is ASCII for this
   reason and tests it. The pwsh 7 files also carry a BOM; keep it when editing.
4. **On 5.1, a native command's stderr** (like git's "From https://…" line) shows up red as a
   `NativeCommandError`. It is harmless, but with `$ErrorActionPreference='Stop'` it can stop the
   script.
5. **KV cache match:** the message array the UI keeps must be byte-for-byte identical to what the
   server actually processed in that turn. If tool calls/results are not kept, or the system prompt
   changes per turn, the context is reprocessed from scratch every turn (40+ s, dropped connections).
   Put per-turn text into the last user message.
6. **Use `Start-DetachedProcess`.** `Start-Process -Redirect…` inherits the caller's stdout pipe and
   `manage-mamidlabs | …` hangs.
7. **The VRAM margin is context-aware and profile-based** (MAX/FLASH 1200 MiB @32K / 800 MiB @16K,
   LITE 300 MiB) and the tolerance is
   LITE 1024 / MAX/FLASH 0. The margin equals the measured model footprint (27B ~15.3 GiB at 16K /
   ~15.8 GiB at 32K on the
   GPU): the model loads only when it fully fits, uses all free VRAM and does not spill; a normal
   desktop leaves only ~15.5 GiB free, so the gate **tries 32K and falls back to 16K** rather than
   loading a known-to-spill 32K. Without the fallback the 27B spilled ~400 MiB into shared memory
   and dropped from ~40 to ~27 tok/s.
   **ComfyUI** by default runs only during media generation (it holds no VRAM/RAM while idle) and is closed before the language model loads; on LITE it can be kept open between media jobs with `comfyKeepAlive=true`. A leaked/duplicate instance is cleaned in every profile (ComfyUI is a process pair, so instance roots - not process count - decide). A leaked instance spills the 27B to 0.89 tok/s. The mmproj runs on the CPU in **all three profiles**
   (`--no-mmproj-offload`). On the GPU it spilled
   ~420 MiB into shared memory on MAX/FLASH (speed 25 → 19 tok/s) and overflowed the 8 GB LITE budget.
   Before the model loads, leftover app-owned **GPU workers** (an idle audio/OCR worker) are also
   closed automatically (`Stop-OrphanWorkers`); since their remaining CUDA contexts can eat the
   margin, the cleanup only targets processes under the install root.
8. **Single image rule:** only one image at a time goes to chat (multiple images only in the
   "multi-image reading mode", via OCR).
9. **Testing with UI Automation:** UIA `Invoke` on a menu item that opens a modal window does not
   return until the window closes and can lock the app's UIA provider. Use Win32
   `PostMessage(BM_CLICK)` for buttons that open modals and the keyboard for menus. Inno Setup
   buttons appear as `Pane` in UIA.
10. **Do not edit `settings\api.json` by hand while the app is open;** the in-memory copy is written
    on exit.
11. **`$input` and `$args`** are PowerShell automatic variables; do not use them as local variable
    names.
12. **An integer key in an ordered dictionary (`[ordered]@{}`)** is interpreted as a position; use a
    plain `@{}` for integer keys.
13. **`$env:TEMP` can return a short path** (`BUYUKA~1`); use `GetFullPath` in path comparisons.
14. **`run_command` does not pass the command as a `cmd.exe /c` argument.** .NET's argv quote
    escaping (`\"`) does not match cmd.exe's parsing, and commands with nested quotes
    (`powershell -Command "if (...) { ... }"`) break. The command is written to a temporary `.cmd`
    file, saved as **UTF-8 without BOM** and starting with `chcp 65001`: cmd.exe does not reliably
    recognize a UTF-8 BOM, and on pwsh 7 `[Text.Encoding]::Default` is already UTF-8 without BOM.
15. **In the bound-folder tools `relative_path` is not passed straight to `Join-Path`.** Small models
    sometimes write a full (rooted) path, producing a doubled, invalid path. `Resolve-BoundTarget`
    uses a rooted path as-is; the `Inside` check keeps the sandbox boundary.
16. **`%` is special in a `.cmd` file.** Every unmatched `%` is silently deleted (`42 % 2` →
    `42  2`); matched references like `%TEMP%` expand correctly. Matched `%[...]%` pairs are kept
    first, then every remaining lone `%` becomes `%%`:
    ```powershell
    [regex]::Replace($command, '%[^%]*%|%', { param($m) if ($m.Value.Length -gt 1) { $m.Value } else { '%%' } })
    ```
    Known limitation: two unrelated `%` on one line (`a % b % c`) can be taken for a pair; the
    command fails (it is not silently wrong) and the model retries with `%%`. cmd.exe's `%` rules can
    only be verified on real Windows.
17. **The OCR "direct path"** (`Test-OcrExplainIntent`) returns the raw OCR text without going
    through the model. If the request asks for an explanation/summary/interpretation or a confidence
    score (`confidence|score`), it goes to the model; otherwise extra requests like the score are lost.
18. **The two language editions cannot share an Inno Setup `AppId`.** The `AppId` is the permanent
    identity Windows uses for the product; TR and ENG carry separate GUIDs.
    `$script:MamidLabsAppId` in `agent/profile-catalog.ps1` must match the `.iss` (a test checks it).
    If a new language/variant is added, give it a fresh GUID; never change the `AppId` of a released
    edition.
19. **Adding a new `Source:` line to the `.iss` does not update the CI path filter.** Every
    file/folder embedded in the package must also be in the push and PR path filters of
    `installer.yml`.
20. **In the wizard, `Exec(..., ewWaitUntilTerminated, ...)` stops the message loop;** the window
    shows "Not Responding" and Cancel does not work. Setup is awaited with `ewNoWait` +
    `PumpMessages` (`PeekMessageW`/`TranslateMessage`/`DispatchMessageW`, a flat 28-byte `TMsg`
    record). While this loop runs, Inno's own Cancel button does **not** fire `CancelButtonClick`
    (verified on real Windows); a `CustomCancelButton` with a normal `OnClick` is shown instead, with a
    fixed caption. In `ssPostInstall`, `Abort` does not stop the wizard, which moves on to the
    Summary/Finished pages; `ExitProcess(1)` is used after a cancel. `LoadStringFromFile` needs an
    `AnsiString` as its second parameter.
21. **An update install cannot complete while the app is open.** `install-mamidlabs.ps1` stops in
    the `layout` stage with "MamidLabs is open" when it sees a running `mamidlabs-desktop.ps1`. That is why
    "Update now" closes the main window after starting the setup wizard (`$mainForm.Close()` →
    `Shutdown-All`). The Profiles window's handlers use `GetNewClosure()`, so the main form is
    captured through a local variable in the function (`$mainForm = $form`).
22. **An empty array comes back as `$null` across the `Start-ThreadJob` boundary.** When no tool is
    called the worker puts an empty array (`@()`) in the `data` of the `done` event; `Receive-Job`
    turns it into `$null`. `@($done.data)` then wraps it into a single-element `@($null)` and adds a
    blank entry to the message list, which the server rejects with
    `400 All non-assistant messages must contain 'content'`. So everywhere that consumes worker
    output, guard with `if ($done.data)` / `if ($jobEvent.data)` first (the `Render-JobEvent` in the
    UI and the `history_resume` smoke step do this). Never concatenate an empty array with `+`.
23. **The audio models live in the install root's HF cache; `HF_HOME` must be set at runtime.**
    `tools\hf-home` (whisper large-v3 + the AST audio-event model) is used with `HF_HOME` only by
    `install-mamidlabs.ps1` during install. The app sets `$env:HF_HOME = <root>\tools\hf-home` in
    `mamidlabs-desktop.ps1`, and `agent/mamidlabs-inference.ps1` passes `Get-HfHome` to both the one-shot
    `Invoke-PythonTool` (child process env) and the persistent `Start-AsrServerProcess`. Without it
    the worker falls back to the DEFAULT cache, AST is not found and voice input fails (see §16).
    When adding a new HF-using worker, do not forget to pass `HF_HOME`.

## 15. Checklist when making changes

- [ ] Run the relevant tests; add a test for new behavior (`python -m pytest -q`).
- [ ] **Pre-release variant gate (mandatory):** before tagging/releasing, in **TR and ENG** repos;
      a full smoke test with the **PRO (MAX/FLASH)** and **LITE** profiles; and the installer's
      (Inno, `MamidLabs_Setup.exe`) TR/ENG build. **No release until all pass.**
- [ ] If you touched the media/chat flow, run the smoke test with the relevant steps.
- [ ] Is user-facing text in English and saved with the right encoding (BOM)?
- [ ] Install/uninstall impact: does a new file or folder write outside the root? If so, add it to
      the uninstaller. By default everything must stay under the root.
- [ ] The install root copy, the `setup-source` refresh, the uninstaller `$programEntries` and the
      `.iss` `[Files]`/`[UninstallDelete]` must stay in sync: when you add or remove a file/folder
      copied into the root (or an exclude such as `agent\__pycache__\*`), update all of them in the
      same change. `tests/test_install_layout_source.py` and `tests/test_uninstall.py` guard this.
- [ ] Are `README.md`, `docs/installation.md`, `docs/technical-notes.md`, `CHANGELOG.md` and **this
      file** up to date?
- [ ] Does the Turkish edition need the same change? Identifiers there are Turkish.
- [ ] If releasing: do the `.iss` `MyAppVersion`, the CHANGELOG section and the `v<version>` tag match?
- [ ] To update an installed copy, close the app, then run `install-mamidlabs.ps1 -Stage layout`.

## 16. Change log

Add one line per feature/change (newest first).

| Date | Change | Files |
|---|---|---|
| 2026-10-17 | **The 32K VRAM spill was fixed with an automatic 32K → 16K context fallback (PRO/FLASH speed back to ~40+ tok/s).** Found by live measurement (llama.cpp's own buffer report, RTX 5060 Ti 16 GB): the 27B has a **real GPU footprint of ~15.77 GiB at 32K** (model buffer 13945 MiB + KV 576 MiB + recurrent-state buffer 598.5 MiB + compute ~112 MiB + CUDA context) but a normal Windows desktop leaves only **~15.50 GiB free**, so the old fixed **750 MiB** margin (threshold 15350) admitted it and **~400 MiB spilled into Windows shared memory, dropping chat from ~40 to ~27 tok/s**. Fix: the margin is now **context-aware** (`Get-ModelOverheadMiB`: 1200 MiB @32K, 800 @16K, 600 below; LITE 300 unchanged) and `Test-MemoryBudget` takes the context; MAX/FLASH **tries 32K first and falls back to 16K** when 32K does not fully fit (same weights, half the KV cache; measured 16K footprint 15324 MiB, ~40 tok/s, no spill). The chosen context feeds both the gate and `--ctx-size`, and the UI **reads the real context back** from the running server (`/props` → `default_generation_settings.n_ctx`) so `ctxLimit`/`compactAt` match and a long turn cannot exceed `--ctx-size`. Version unchanged (`MyAppVersion` stays 1.0.0). Tests: `tests/test_memory_admission.py` (context-aware margin + fallback), `tests/test_lite_profile.py` (locked PRO args use `$modelCtx`), `tests/test_desktop_behavior.py` (UI read-back). | scripts/manage-mamidlabs.ps1, mamidlabs-desktop.ps1, tests/test_memory_admission.py, tests/test_lite_profile.py, tests/test_desktop_behavior.py, docs/technical-notes.md, CHANGELOG.md, AGENTS.md |
| 2026-10-16 | **The README's Install column now shows the real file names (documentation only).** The Linux/macOS rows read `mamidlabs.deb` / `MamidLabs.dmg` instead of `.deb (setup wizard)` / `.dmg (setup wizard)`, matching the Windows `MamidLabs_Setup.exe` row; the links still lead to the direct download. | README.md, CHANGELOG.md, AGENTS.md |
| 2026-10-16 | **Simplified the README's platform section + download links (documentation only).** The platform badges ("labels") under the title were removed; the arrow and the separate repository link on the "Linux → repo" / "macOS → repo" lines were removed, the "Repository" column was dropped from the table. The **Linux** and **macOS** words are clickable (matching repository); `MamidLabs_Setup.exe`, `.deb` and `.dmg` lead to the **direct download** (`releases/latest/download/...`). The "Also available for Linux and macOS" section was removed. Workflows were added to the `mamidlabs_linux`/`mamidlabs_mac` repos that publish fixed-name packages (`mamidlabs.deb`/`MamidLabs.dmg`) on a `v*` tag. | README.md, CHANGELOG.md, AGENTS.md |
| 2026-10-16 | **Fixed the flakiness of `test_python_tool_sets_hf_home_for_child` (it gave a false red once in CI).** The test read the `HF_HOME` that `Invoke-PythonTool` gives the child only from pwsh's own stdout; on a loaded runner that output occasionally came back empty and the test failed **without a real regression** (the same commit passed 8/8 locally). The child now also writes `HF_HOME` to `hf_home.txt` beside the probe, the primary assertion reads that file, and pwsh's own output is verified as well when available. The measurement therefore comes from the child's real environment, not from channel noise. Applied to the Turkish edition too. | tests/test_transcriber.py, AGENTS.md, CHANGELOG.md |
| 2026-10-16 | **Added a "Platform support" section and platform badges to the README (documentation only).** The Linux and macOS ports are marked **experimental**; badges under the title link to the `mamidlabs_linux` / `mamidlabs_mac` repositories in one click, plus an "**Also available for Linux and macOS**" section (same tool set + `.deb`/`.dmg` setup wizard). The quick navigation links to the new section. The Windows edition stays stable in this repository. The Turkish-edition link was renamed to "Türkçe için tıklayınız". | README.md, CHANGELOG.md, AGENTS.md |
| 2026-10-16 | **A partial web failure is no longer reported as "the search failed" (found in a real turn).** In the user's weather turn 4 of 7 web calls succeeded (one page 403, one query "no results", one page connection error) yet the answer still got the note "[The web search failed; the information in the answer could not be verified from a current source.]". Root cause: the "web access failed" decision keyed only on `$script:webToolFailCount -gt 0`, so a single failed call was enough. The decision now requires **web was tried AND never succeeded** (`$script:webToolFailCount -gt 0 -and !$script:webToolSuccess`) and is applied consistently in four places: (1) `Add-WebFailureNote` (the visible note; returns early when `webToolSuccess`), (2) the `run_command` network-probe refusal, (3) the main loop's "no internet" system note, (4) locking the web tools after ≥2 failures. If at least one call returned data the answer rests on a real source. Test: `tests/test_generation_intent_guard.py` (partial success → no note + citations kept). | agent/mamidlabs-inference.ps1, tests/test_generation_intent_guard.py, CHANGELOG.md, AGENTS.md |
| 2026-10-16 | **Legacy engine files renamed away no longer linger on an update (upgrade cleanup).** When the engine/plumbing names moved to MamidLabs, the top-level `qwen-desktop.ps1` / `launch-qwen-desktop.vbs` files stayed behind in existing installs (Inno `ignoreversion` / `Copy-Item -Force` does not delete the old file; renames inside folders were already cleaned when the owned folders are pruned). `scripts/install-mamidlabs.ps1`: `Remove-LegacyTopFiles` removes those names from the root and `setup-source`; the .iss `[InstallDelete]` (upgrade) + `[UninstallDelete]` (uninstall) and the uninstaller `$programEntries` target the same names. Test: `tests/test_install_layout_source.py`. | scripts/install-mamidlabs.ps1, scripts/uninstall-mamidlabs.ps1, packaging/windows/mamidlabs.iss, tests/test_install_layout_source.py, AGENTS.md, CHANGELOG.md |
| 2026-10-16 | **Engine/plumbing names moved to the product name (MamidLabs); "qwen" now remains only in genuine model identity.** The product is not named "Qwen", so internal engine/plumbing names are named after the product. Renamed files: `qwen-desktop.ps1`→`mamidlabs-desktop.ps1`, `launch-qwen-desktop.vbs`→`launch-mamidlabs-desktop.vbs`, `agent/qwen-inference.ps1`→`agent/mamidlabs-inference.ps1`, `agent/qwen-agent-tools.py`→`agent/mamidlabs-agent-tools.py`, `scripts/manage-qwen.ps1`→`scripts/manage-mamidlabs.ps1`, `tests/test_qwen_contracts.py`→`tests/test_mamidlabs_contracts.py`; runtime contracts: `QwenWindow`→`MamidLabsWindow`, `Stop-LocalQwen`→`Stop-LocalMamidLabs`, `Restart-LocalQwen`→`Restart-LocalMamidLabs`, `Get-ManageQwenPath`→`Get-ManageMamidLabsPath`, mutex `Local\QwenLocalModelControl`→`Local\MamidLabsModelControl`, log pattern `qwen-<profile>.std{out,err}.log`→`mamidlabs-<profile>.std{out,err}.log`, User-Agent `QwenLocal/1.0`→`MamidLabs/1.0`, wizard option `OptQwen38`→`OptPro`. **Kept (genuine model identity):** profile aliases (`qwen3.8-27b-max`/`flash`, `qwen3.5-9b-lite`), GGUF/mmproj filenames, HF repo ids, ComfyUI files (`qwen3vl_4b_fp8_scaled.safetensors`, `qwen_image_vae.safetensors`, `TextEncodeQwenImageEditPlus`), `qwen35.*` arch keys and text that names the model. Tool names are unchanged (none contained "qwen"). Installer/uninstaller, CI path filters and docs (including past §16 rows) were updated with the new names. pytest TR 476 / ENG 473 green. | mamidlabs-desktop.ps1, launch-mamidlabs-desktop.vbs, agent/mamidlabs-inference.ps1, agent/mamidlabs-agent-tools.py, scripts/manage-mamidlabs.ps1, tests/test_mamidlabs_contracts.py, packaging/windows/mamidlabs.iss, .github/workflows/installer.yml, AGENTS.md, CHANGELOG.md |
| 2026-10-16 | **Every tool is offered the same way in every profile (except identity editing).** From the "all tools must work in all profiles" review: `Get-ToolDefinitions` is profile-independent — MAX/FLASH/LITE all see the same tool set (the intent/chip gates do not vary by profile). The only profile difference is `identity_edit` (present on MAX/FLASH, absent on LITE; 5 + 7.5 GB > 8 GB VRAM, see §13), guarded in three layers: the tool is not offered (`$identityEditAvailable = -not $job.imageProfileLite`), `imagegen-run.ps1` rejects it and the LITE catalog has no LoRA. The behavior is now locked by `test_tool_set_is_identical_across_profiles_except_identity_edit` in `tests/test_lite_profile.py` (a real pwsh compares the two profiles' tool-name sets and the `generate_image` mode list). | tests/test_lite_profile.py, CHANGELOG.md, AGENTS.md |
| 2026-10-16 | **Automatic VRAM cleanup before the model loads (MamidLabs processes only).** Leftover/leaked app-owned GPU workers — an idle persistent audio worker (`whisper-tools.py --serve`) and the OCR worker (`ocr-tools.py`) — are closed automatically **before** the language model loads: after a crash or forced close their remaining CUDA contexts (~140–600 MiB) could eat the 27B's thin margin and cause a "not enough VRAM" error. `scripts/manage-mamidlabs.ps1`: `Get-AppGpuWorkers` (selects only python processes under the install root running those two GPU-worker scripts; ComfyUI `main.py` has its own lifecycle) + `Stop-OrphanWorkers` (closes them and waits for the driver to release VRAM) run BEFORE the memory gate; the gate's retry count went 6→8 (~7 s). Other applications' processes are never touched. Tests: `tests/test_memory_admission.py` (static call ordering + process selection via a real pwsh). | scripts/manage-mamidlabs.ps1, tests/test_memory_admission.py, CHANGELOG.md, AGENTS.md |
| 2026-10-15 | **Voice input was broken on a normally launched app: `HF_HOME` was never set at runtime.** The audio models (whisper large-v3 + AST) live in the install root's HF cache (`tools\hf-home`) and the installer sets `HF_HOME` there **during install only**. At runtime the app did not set it, so the worker fell back to the DEFAULT cache (`%USERPROFILE%\.cache\huggingface`); the AST model is not there, so `events.tag()` raised while loading it and the **whole transcription failed** with "Can't load feature extractor" (events default ON; only the "no events" case was handled). Fix: (1) `mamidlabs-desktop.ps1` sets `$env:HF_HOME = <root>\tools\hf-home` at startup; `agent/mamidlabs-inference.ps1` gained `Get-HfHome` and both the one-shot `Invoke-PythonTool` (child `HF_HOME`) and the persistent `Start-AsrServerProcess` use that cache (so it is offline, no network needed); (2) `events.tag()` guards the AST load with `try/except`, so if the model is ever missing the events are skipped (empty list) and the transcript still succeeds. Tests: `tests/test_transcriber.py` (static guarded load + `tag` empty-list behavior + a real-pwsh check that `Invoke-PythonTool` hands the child `HF_HOME` + static wiring). | mamidlabs-desktop.ps1, agent/mamidlabs-inference.ps1, agent/transcriber/events.py, tests/test_transcriber.py, CHANGELOG.md, AGENTS.md |
| 2026-10-15 | **Voice-input latency: a warm (persistent) audio transcription worker.** The fixed cost repeated on every audio turn (Python start-up + torch/transformers/faster-whisper imports; measured ~10 s, ~25 s cold/disk) is now paid ONCE per session: `whisper-tools.py --serve` runs as a single persistent process listening on 127.0.0.1 (port/pid in `logs\asr-server.json`) and holds the models in-process; `{"action":"unload"}` drops the audio models (the process stays alive). Before the language model is reloaded into VRAM the worker is unloaded and STOPPED (its remaining CUDA context ~140 MiB could eat the 27B's thin margin) and restarted IN THE BACKGROUND after the turn; it exits itself after 600 s idle / when the app closes. If the worker is unreachable it falls back to the one-shot `Invoke-PythonTool` path. The one-shot stdin/stdout contract is preserved. Tests: `tests/test_transcriber.py` (serve contract + `_dispatch` + `_parent_gone` + a real `--serve` ping/idle-exit + static PS wiring). | agent/whisper-tools.py, agent/mamidlabs-inference.ps1, tests/test_transcriber.py, CHANGELOG.md, AGENTS.md |
| 2026-10-15 | **Documentation/attribution cleanup (no behavior change).** Removed the license-comparison sentence from `README.md`, the external AI tool names and the `CLAUDE.md` row from the `AGENTS.md` intro; deleted `CLAUDE.md`; neutralized the `tests/ui-automation` temp/screenshot path to `%TEMP%\mamidlabs\ui`; changed the consumer-assistant name in the `docs/technical-notes.md` subscription-login sentence to the product provider name. The API providers (a product feature) are kept. | README.md, AGENTS.md, docs/technical-notes.md, CLAUDE.md (deleted), tests/ui-automation/ui.ps1, tests/ui-automation/win32.ps1, tests/ui-automation/README.md |
| 2026-10-15 | **Prompt-preprocessing latency diagnosed + "use all free VRAM / no spill" fix.** Measurement (RTX 5060 Ti, MAX): the KV cache **works** (turn 2 reused 19,988 tokens, first token 0.6 s) and prompt processing is ~415 tok/s on a clean load; a **full reprocess** of a long context is ~48 s (20k tokens). The ~20 tok/s turn was ~20x slower because the model did not fully fit VRAM and **spilled into shared memory**. The **measured model footprint is 15350 MiB** (file 14,600 but on the GPU with q4_0 KV + compute buffers + MTP draft context; a clean load at 15454 MiB free gives ~41-55 tok/s). The MAX/FLASH margin is now **750 MiB** (threshold 15350): the model loads only when it **fully fits**, **uses all free VRAM** and **does not spill**; on a normal desktop (browser closed) it **loads without error**; below that a clear "not enough VRAM" is shown. `--gpu-layers 99` is kept; llama.cpp's `--fit` auto-placement was tested and proved more **conservative** (did not use all VRAM, ~23 tok/s) -> not used. (The earlier "slow mode warning" approach was removed: spill is prevented directly.) | scripts/manage-mamidlabs.ps1, tests/test_memory_admission.py, AGENTS.md, CHANGELOG.md |
| 2026-10-14 | **The UI shows the real model-load failure reason.** `Invoke-Manage` now captures the manager process's stderr via `RedirectStandardError`; `Update-ModelSwitch` runs the new `Format-ModelSwitchError` on a failed exit to show the meaningful line (e.g. "Not enough free VRAM: …") instead of only the generic "Model operation failed". Measurement note: the MAX/FLASH memory tolerance stays **0** — the 27B fully fits with 15244 MiB free (~45 tok/s) but spills at 15095 MiB (~30 tok/s), so an overflow slows it down and blocking it is correct. Test: `tests/test_desktop_behavior.py`. | mamidlabs-desktop.ps1, tests/test_desktop_behavior.py, AGENTS.md, CHANGELOG.md |
| 2026-10-14 | **Restored the anti-fabrication clause that the system-prompt rewrite dropped (caught by tests).** The earlier "strict anti-fabrication" rewrite removed "Do NOT use the name of a test, theorem, identity or source unless you are sure it exists" (and "review your critical steps") from the fixed system prompt; `tests/test_mamidlabs_contracts.py` / `tests/test_lite_profile.py` failed. It is restored (the prefix stays fixed, so the KV cache is not broken). pytest now green: TR 462 / ENG 459. | mamidlabs-desktop.ps1, AGENTS.md, CHANGELOG.md |
| 2026-10-14 | **The installed program files are byte-identical to the repo (the `assets\installer` exclude was removed).** `layout` and the `setup-source` refresh no longer drop `assets\installer\*` (wizard art); the `.iss` `[Files]` `Excludes: "installer\*"` was removed and `Remove-ExcludedFromOwned` now only strips `agent\__pycache__\*`/`*.pyc`. So the install root's program files (`agent\`, `scripts\`, `assets\`, `requirements\` and the top-level files) match the repo byte-for-byte. | scripts/install-mamidlabs.ps1, packaging/windows/mamidlabs.iss, tests/test_install_layout_source.py, AGENTS.md, CHANGELOG.md |
| 2026-10-14 | **Strict anti-fabrication + no generation tools on a web-research turn.** (1) A broad current/time-sensitive set; without web access the text is not shown and it says "this information is not something I can get without an internet connection". (2) Web tried-but-failed strips fabricated "Source:"/URL lines (`Remove-FabricatedCitations`). (3) `Test-CurrentInfoIntent` widened; the system prompt answers definite knowledge confidently but never fabricates current/unverifiable info. (4) On a web-research turn the image/video/document/chart tools are not offered unless explicitly requested. Also `Get-FriendlyErrorMessage` on a dropped stream + thinking-only drop routes to recovery. | agent/mamidlabs-inference.ps1, mamidlabs-desktop.ps1, tests/test_generation_intent_guard.py, CHANGELOG.md, AGENTS.md |
| 2026-10-14 | **README/AGENTS product summary updated (no behavior change).** `README.md` gained the chart-drawing capability (intro line + Features table) and the new safety behaviors (no unprompted document/image/video/chart generation; if a web search fails, or the internet is off for a current-information request, an honest report instead of a fabricated answer; `read_document` only with a document context). `AGENTS.md` §1 product summary was aligned with chart drawing. | README.md, AGENTS.md, CHANGELOG.md |
| 2026-10-14 | **Unprompted generation blocked + failed web search reported honestly + `read_document` needs a document context.** The generation tools (`create_document`, `generate_video`, `generate_chart`) were offered on every turn with only the system prompt as a guard; LITE generated unprompted. They now have the same **call-time intent guard** as `generate_image` (`Test-DocumentRequestIntent`/`Test-VideoRequestIntent`/`Test-ChartRequestIntent`; video is also checked in the block before handing over to the UI). **Fabrication is completely eliminated:** when a current-information turn has no web access (search failed or Internet off) the model text is **not used**; `Get-FinalAnswer` shows an honest message (`Add-WebFailureNote` appends a note when a search failed and current info was not required). An empty answer is replaced by a clear message; the same generation tool runs once per turn (`$script:completedTools`). `read_document` is offered only with an attached document or `Test-FileIntent` (the model invented a non-existent file and read it). Test: `tests/test_generation_intent_guard.py` (real pwsh + static). | agent/mamidlabs-inference.ps1, tests/test_generation_intent_guard.py, CHANGELOG.md, AGENTS.md |
| 2026-10-14 | **"Delete all" keeps the open chat.** "Delete all" in the Past chats window used to wipe the whole list (including the chat on screen); the open chat's file is now excluded from the delete set (`Get-DeletableChatPaths`), the confirmation says "The open chat is kept." and the open chat stays in history. Test: `tests/test_chat_history_delete.py` (real pwsh + static guard). | mamidlabs-desktop.ps1, tests/test_chat_history_delete.py, CHANGELOG.md, AGENTS.md |
| 2026-10-13 | **PRO (MAX/FLASH) arguments locked + §12 test figures corrected.** Added `test_max_flash_server_args_are_locked` in `tests/test_lite_profile.py`: it locks the whole MAX/FLASH (`else`) server-argument tuple (ctx 32768, predict 8192, `--gpu-layers 99`, flash-attn, q4_0 KV, cache-reuse 256, batch 512/256, threads 6/12, `--mmproj --no-mmproj-offload --image-min-tokens 1024`, `--reasoning-effort` xhigh/low, `--spec-type draft-mtp` when the GGUF has MTP). That tuple was smoke-verified on a real 16 GB machine and a silent edit could break PRO. Also set the §12 test count/time to the real value. | tests/test_lite_profile.py, AGENTS.md |
| 2026-10-13 | **On an audio turn the bound-folder FILE tools are no longer offered (audio attachment + Agent chip).** A turn with an audio attachment already transcribes it with faster-whisper and adds the text to the last user message; but with the Agent chip on the small model read the `[The user attached an audio file: …]` note and tried to find/read/write the audio FILE with `list_bound_files`/`read_bound_file`/`write_bound_file`, wasting turns (a list/write loop in the UI test, with confusing "treated as a document" text). `Get-ToolDefinitions` now does not offer those three tools on an audio turn (`$job.currentAudioPaths`); if the user explicitly asks for a file (`Test-FileIntent`) they are still offered. A successful transcript note (`Add-TranscriptToMessages`) also tells the model it is an audio transcript and not to open the file with tools (symmetric to the `Set-AudioUnavailableNote` failure note). Version 1.0.0. | agent/mamidlabs-inference.ps1, tests/test_transcriber.py, CHANGELOG.md, packaging/windows/mamidlabs.iss, AGENTS.md |
| 2026-10-13 | **Installer (CI) release-publish race fixed.** The GitHub Release step in `installer.yml` could have two concurrent runs for the same tag; both saw the release as missing and called `gh release create`, so the second failed with **HTTP 422 "Release.tag_name already exists"** (the release was still created by the first run; only one run showed red). The step is now atomic: if `create` fails it re-checks whether the tag's release exists and, if so, uploads with `--clobber`. Also corrected stale pre-1.0.0 comments in `manage-mamidlabs.ps1` (old "7500 MiB / ~41 tok/s / mmproj on the GPU" figures) to the current CPU-projector reality (7249/8188 MiB, ~40–79 tok/s). | .github/workflows/installer.yml, scripts/manage-mamidlabs.ps1 |
| 2026-10-13 | **Documentation/comment-consistency pass (docs and comments only; no behavior change, `MyAppVersion` stays `1.0.0`).** A final audit fixed stale or hard-coded documentation figures: the `.iss` header comment no longer hard-codes `(v1.0.0)` (it now says CI compares the pushed tag `v<version>`); the `asr` stage was added to the AGENTS §10 and `docs/installation.md` stage lists to match `$script:KnownStages`; `requirements/asr.txt` was added to the AGENTS §2 map; the MTP acceptance figure was reconciled to **~0.93 (content-dependent 0.31–0.96)** in AGENTS §5 and `docs/technical-notes.md`; the LITE size comments in `scripts/manage-mamidlabs.ps1` now agree (`~6.5 GB + ~0.46 GB MTP head`); the `chart` (`-SkipChart`) smoke step was added to AGENTS §12; `README.md` gained a "For Türkçe" link mirroring the TR edition's "For English" link; the `agent/transcriber` `__version__` was documented as a package version independent of the app's `.iss MyAppVersion`; the §12 test figures were set to the real value (**450 collected / 446 passed / 4 skipped, ~223 s**) and the "documentation consistency" row below is now explicitly framed as the value **at that time** (mmproj on the GPU, before it moved to the CPU) so it no longer contradicts the 7249/8188 MiB row; and the LITE "comfortable margin" wording in `docs/technical-notes.md` was reconciled with the deliberately small 300 MiB admission gate. | packaging/windows/mamidlabs.iss, AGENTS.md, README.md, docs/installation.md, docs/technical-notes.md, scripts/manage-mamidlabs.ps1, agent/transcriber/__init__.py, CHANGELOG.md |
| 2026-10-13 | **Version/updater parity with TR: only a strict `x.y.z` `MyAppVersion` is accepted, and the wizard's nested source no longer breaks `layout`.** `Get-AppVersionFromSource` now matches `#define\s+MyAppVersion\s+"(\d+\.\d+\.\d+)"` and returns `$null` otherwise (a non-semver `MyAppVersion` was previously written as `DisplayVersion` and broke the in-app updater's `[version]` comparison); a non-`x.y.z` value now writes no `DisplayVersion`. The `$sameRoot` guard is equality-only (so the wizard's nested `-Source <root>\setup-source` still copies the program files into the root), and the `setup-source` refresh is skipped when the source IS `setup-source` (copying the package onto itself emptied it and stopped the script). New functional tests (strict-semver; nested-source; install-root-without-source) and a static guard test in `tests/test_install_layout_source.py`. | scripts/install-mamidlabs.ps1, tests/test_install_layout_source.py, AGENTS.md, CHANGELOG.md |
| 2026-10-13 | **Install/update correctness: the script install and the in-app updater now agree, and stale/leftover files are handled.** (1) `Invoke-Layout` writes `DisplayVersion` (from `-AppVersion` or `MyAppVersion` in the `.iss` under `-Source`, `x.y.z`) into its `MamidLabs-<hash8>` uninstall entry, and `Get-InstalledMamidLabsVersion` falls back to that script key (matched by `InstallLocation`/`DisplayName`) when Inno's `{AppId}_is1` key is absent — previously a script install had no readable local version and "Check for updates" assumed an update every time. (2) The owned program folders (`agent\`, `scripts\`, `assets\`, plus `requirements\`) are pruned before copying in `layout` **and** in the `setup-source` refresh, so files removed from the source no longer linger; user data is never touched. (3) `requirements\` was missing from the install root: it is now in the root folder creation and copy lists. (4) The `.iss` excludes (`agent\__pycache__\*`, `*.pyc`, `assets\installer\*`) are applied in the script copy and the bundle refresh so the root matches the wizard package. (5) A repeated `layout` skips writing the script uninstall key when a matching Inno `{AppId}_is1` key already exists (no duplicate Apps entry). (6) The uninstaller now removes `EULA.txt` and `requirements\` (added to `$programEntries`; the `.iss` gained `Type: files; Name: "{app}\EULA.txt"`). AGENTS §3 lists `EULA.txt`/`requirements\`; new tests in `tests/test_uninstall.py`, `tests/test_install_layout_source.py` and `tests/test_profile_update_check.py`. | scripts/install-mamidlabs.ps1, agent/profile-catalog.ps1, scripts/uninstall-mamidlabs.ps1, packaging/windows/mamidlabs.iss, tests/test_uninstall.py, tests/test_install_layout_source.py, tests/test_profile_update_check.py, AGENTS.md, CHANGELOG.md |
| 2026-10-13 | **LITE vision projector moved to the CPU so the 9B model fits in 8 GB of VRAM without overflow.** The Q8_0 mmproj (595 MiB) did not fit on an 8 GB card and pushed llama.cpp's large embedding tensor onto the CPU, running VRAM over the limit; with `--no-mmproj-offload` the projector now runs on the CPU in all three profiles. Used VRAM drops from **7885 to 7249 MiB** (free **72 → 708 MiB**); chat speed is unchanged (~40–79 tok/s) and only image reading is slightly slower. `Test-MemoryBudget` no longer adds the projector size to the VRAM budget. | scripts/manage-mamidlabs.ps1, agent/model-registry.ps1, agent/media-model-swap.ps1, tests/test_lite_profile.py, tests/test_memory_admission.py, AGENTS.md, CHANGELOG.md |
| 2026-10-13 | **Documentation consistency: `docs/technical-notes.md` was aligned with the current code.** The MTP section was fixed: the LITE 9B GGUF loses its MTP/nextn head during conversion and gets it re-injected on download via `agent/mtp_inject.py`, so `--spec-type draft-mtp` is used on LITE too (same as §5). The LITE memory budget was updated to **7885/8188 MiB** (mmproj on the GPU at that time; the projector was moved to the CPU afterwards) and its speed to **content-dependent ~40–79 tok/s** (~97 on 16 GB); the `reasoning` value in the LITE row was changed from `low` to **unlimited**. The ComfyUI lifecycle was corrected to `comfyKeepAlive` **default `false`** (runs only during media generation, holds no VRAM/RAM while idle); the same fixes were applied to `README.md`, `docs/installation.md` and `CHANGELOG.md`. The MAX/FLASH summary speeds in `agent/profiles.json` were set to ~42–58 tok/s; `closecomfy` was added to the `-Action` list in `docs/installation.md`. The §12 test count/time was set to the real value (~450 tests, ~223 s; 446 passed, 4 skipped). | docs/technical-notes.md, README.md, CHANGELOG.md, docs/installation.md, agent/profiles.json, AGENTS.md |
| 2026-10-12 | **Fixed the empty visible-answer regression on hard questions: thinking consumed the whole output budget.** In an end-to-end math test LITE (9B), on a hard integral question, spent **all 8192 tokens** (~159.7 s) thinking and produced **no visible answer**; the bubble kept only "İntegral" (`logs\mamidlabs-lite.stderr.log`: `eval 159714 ms / 8192 tokens`, `n_tokens 11990`). Root cause: thinking is "unlimited", the output cap `--predict 8192` and the UI `maxTokens 8192` **share the same budget**; the "double-check the steps TWICE" instruction made thinking even longer and the existing empty-answer fallback was stuck at the same 8192. (1) **Thinking-off recovery (the real guarantee):** the new `Set-NoThinkingRequest` puts `chat_template_kwargs = @{ enable_thinking = $false }` into the request body (the only guaranteed way, since LITE ignores `reasoning_effort`); the recovery message also gets `/no_think`, so the model writes the answer directly. The trigger was widened: it fires when the visible answer is **empty** OR was cut off at the output cap (`finishReason='length'`) leaving a broken short fragment. (2) **System prompt softened (TR+ENG):** "check at least TWICE" → "review the critical steps again BUT ALWAYS WRITE YOUR ANSWER; do not think endlessly and end up with no answer"; the LITE deep-thinking line was updated so it no longer encourages endless thinking. (3) **Budget decision:** since thinking is unlimited, raising `--predict` does **not** guarantee an answer and only lengthens thinking and latency (measured: 8192 tokens = 159.7 s); so (b) [guaranteed recovery] was chosen over (a), with no KV/context overflow. New regression tests: `test_empty_answer_recovery_disables_thinking` (static + real pwsh), system-prompt-softening assertions. | agent/mamidlabs-inference.ps1, mamidlabs-desktop.ps1, tests/test_transcriber.py, tests/test_lite_profile.py, tests/test_mamidlabs_contracts.py, CHANGELOG.md, AGENTS.md |
| 2026-10-12 | **Block LaTeX → readable text + a fixed "evidence-based / double-check" system instruction (found in an end-to-end math test).** (1) `Convert-MathText` now resolves block structures with a **balanced** parser (`Read-MathGroup`/`Read-MathAtom`/`Add-MathAccent`/`Expand-MathCommands`); a plain regex left raw `\frac`/`\boxed` because of nested groups and escaped `\{ \}` (even the final boxed result showed as `boxed{...}`). Supported: nested/parenthesised and escaped `\frac{A}{B}` → `(A)/(B)`, `\sqrt{X}`/`\sqrt[n]{X}` → `√(X)`/`ⁿ√(X)`, `\boxed`/`\fbox` → `[ X ]`, `\textbf`/`\textit`/`\mathrm`/`\text`/`\operatorname` → content, `\left`/`\right` → plain delimiters, `\overline`/`\bar`/`\vec`/`\hat` → combining marks, `\begin`/`\end` dropped. Example: `\boxed{\frac{\pi^5(89+72\sqrt3)}{3888}}` → `[ (π⁵(89+72√(3)))/3888 ]`. The glyph table, `\ce`/`\pu` and the currency `$` are kept. (2) A FIXED (not per-turn; the KV cache is not broken) verification line was added to `Get-SystemPrompt`: evidence-based/correct answers + check the steps and symbolic/numeric **intermediate results at least twice** in multi-step math/algebra/logic + **no fabricated test/theorem/source names**; the LITE deep-thinking line was merged with it (no duplication). Tests extended; TR 432 / ENG 431 passed. | mamidlabs-desktop.ps1, tests/test_math_render.py, tests/test_mamidlabs_contracts.py, tests/test_lite_profile.py, CHANGELOG.md, AGENTS.md |
| 2026-10-12 | **Audio: LITE CPU/GPU policy (short+RAM -> CPU co-resident, long or insufficient RAM -> GPU+swap), GPU runtime DLLs and the transcribe timeout.** (1) `config.py`: `LITE_CPU_MAX_SECONDS=15` and `choose_plan` — on LITE the CPU co-resident path is chosen ONLY when the recording is SHORTER than the threshold **AND** the pipeline fits in system RAM (`audio_ram_mib`=5120 MiB) -> `('cpu', False)`; for a long recording or insufficient RAM -> `('cuda', True)` (mandatory swap). PRO (20 s) is unchanged; the CPU is never a forced choice (last resort when there is no GPU). Measurement verdict (two independent measurements conflicted; the more DEFINITIVE one, measurement-2, actually ran the LM and the ASR CPU pipeline TOGETHER): LITE co-resident **DOES NOT FIT** (physical 15.25 GB < requirement ~16.1 GB; zero/negative margin, peak ~15127/15611 MB; no OOM but the LM's ~6.1 GB model pages are evicted from the reclaimable cache and re-read from disk on the NEXT chat turn). So the LITE `audio_ram_mib` is kept at **5120 MiB** (`LITE_AUDIO_RAM_MIB`, the SAME safe gate as PRO); with the LM loaded `choose_plan` does NOT pick the CPU branch -> GPU + swap; on a RAM-rich machine a short recording can still use the CPU co-resident path. The swap status text was also made truthful (the language model is not moved to RAM; `manage-mamidlabs -Action stop` STOPS the process). (2) `requirements/asr.txt` gained `nvidia-cublas-cu12` + `nvidia-cudnn-cu12` (CTranslate2 4.x needs `cublas64_12.dll`/`cudnn_*64_9.dll`; absent in torch cu130) -> **GPU ASR now works**; `asr.py` `_add_cuda_dll_dirs` adds the DLL dirs. (3) `mamidlabs-inference.ps1`: `Get-AudioTimeoutSeconds` + `timeoutSeconds` on the transcribe request (`max(600,d*4+300)`<=3600). TR+ENG tests updated. | agent/transcriber/config.py, agent/mamidlabs-inference.ps1, requirements/asr.txt, tests/test_transcriber.py, tests/test_requirements_files.py, AGENTS.md, CHANGELOG.md |
| 2026-10-12 | **Audio transcription: the CPU fallback never ran because of a PowerShell dictionary (hashtable) bug.** `Invoke-TranscribeWithModelSwap` built the CPU retry payload with `$payloadBase + @{ device = 'cpu' }` when the primary (GPU) device failed; in PowerShell a Hashtable `+` **throws on a duplicate key** ("Item has already been added. Key in dictionary: 'device'") because `$payloadBase` already contains `device`/`need_swap`. So the worker never actually fell back to CPU, the real ASR error was swallowed and the transcript disappeared (e.g. when `cublas64_12.dll` is missing the GPU path always errors). The payload is now cloned and the field overwritten (`.Clone()`); the same fix applies to the new "unload the model and retry" block. `tests/test_transcriber.py` guards this pattern. | agent/mamidlabs-inference.ps1, tests/test_transcriber.py, AGENTS.md, CHANGELOG.md |
| 2026-10-12 | **Hygiene: audit-log rotation and a reasoning (thinking) repeat guard (F3/F4).** (F3) `logs\audit.jsonl` was growing without bound; before a write, if the file exceeds 5 MB it is rotated to `audit.1.jsonl` (overwriting any previous one; a single rotation file is enough) and the new line goes to the clean file. The rotation is inside the existing `try/catch`: audit writing **never fails the turn**, even on a rotation error. (F4) The runaway-repeat detector used to look only at the visible content (`$result.content`); because thinking is **unlimited** on LITE, if the model started repeating in the `reasoning_content` stream it spun to the 8192-token cap without a visible answer. The **same threshold/pattern** (the same word 6+ times) is now applied conservatively to the reasoning accumulation too; on strong repetition the stream is stopped at a safe point. If there is no visible answer the existing "empty answer"/low-effort retry path still applies unchanged. F5/D6/D7 were not addressed in this change. | agent/mamidlabs-inference.ps1, tests/test_transcriber.py, AGENTS.md, CHANGELOG.md |
| 2026-10-12 | **Agent-loop resilience: in-turn context overflow, wall-clock limit and `custom`-provider tolerance (F1/F2/D2/D3).** (F1) **Context overflow:** tool outputs accumulating during a turn could push the prompt past `contextLimit`/`--ctx-size` and, because of `--no-context-shift`, llama.cpp returned an ERROR and the turn failed; now the main loop, at the START of every turn, computes the estimated prompt size (`Estimate-MessagesTokens`, the same chars/3 approach as the desktop's `Estimate-ContextTokens`), and if `estimate + max_tokens + reserve > contextLimit` it shortens the **oldest tool outputs** with `Compress-TurnMessages` (keeping the system message and the last user message, WITHOUT deleting messages — the assistant `tool_calls`/`tool` pairing is not broken); if it still does not fit it stops offering tools and asks for a visible answer from the findings at hand. An "Approaching the context limit…" status note is shown to the user. (F2) `max_tokens` is capped to the remaining room (`Min(maxTokens, Max(256, contextLimit - promptTok - reserve))`; local+custom). (D2) `Invoke-PythonTool` now carries a wall-clock limit (default 180 s; overridable by a parameter or `$payload.timeoutSeconds`); when exceeded the subprocess tree is killed with `Kill($true)` and an error is returned, while cancel behavior is preserved. (D3) `custom` (an OpenAI-compatible custom endpoint) also retries **once** on a transient connection error and does not treat a drop after finish/content/tool data as an error; `openai/claude/gemini/deepseek` behavior is preserved. | agent/mamidlabs-inference.ps1, tests/test_transcriber.py, tests/test_mamidlabs_contracts.py, AGENTS.md, CHANGELOG.md |
| 2026-10-12 | **Fixed a wasted turn when an attached audio file could not be transcribed (found in an end-to-end audio test).** When the worker could not get a transcript (error or empty result) it silently continued "without a transcript" and the `[The user attached an audio file: <name>]` note alone remained in the user message; the model took the file for an "attached document", tried to open the `.wav` with `read_document`/`read_bound_file`, and spent **8/8 agent turns** without ever producing a transcript. `Invoke-TranscribeWithModelSwap` now, when the plan says "no swap" yet no transcript is obtained, unloads the language model and retries once with a **clean GPU + more free RAM** (under WDDM the free VRAM the plan reads can be optimistic while the model is loaded; see §13 and pitfall 7); if it is still failing/empty the new `Set-AudioUnavailableNote` adds an explicit note to the last user message saying the audio is **not a document** and could not be transcribed. The engine itself was sound (calling `whisper-tools.py` directly works); the flaw was the worker swallowing the failure and leaving the model with the wrong context. `tests/test_transcriber.py` guards the contract. | agent/mamidlabs-inference.ps1, tests/test_transcriber.py, AGENTS.md, CHANGELOG.md |
| 2026-10-12 | **Script-based installation/update now copies `EULA.txt` to the root and the `setup-source` package.** The wizard put `EULA.txt` into the source package (`.iss` `[Files]`) but it was missing from the upper-file list that `install-mamidlabs.ps1` `Invoke-Layout` copies, so script-based (no-wizard) updates had no license file at all. Added to both upper-file lists; the gap is covered by `tests/test_install_layout_source.py`. | scripts/install-mamidlabs.ps1, tests/test_install_layout_source.py, AGENTS.md |
| 2026-10-12 | **The smoke test's `ui` step now uses a realistic `maxTokens` (300 → 8192).** Because thinking is unlimited on LITE (no `--reasoning-budget`; `reasoning_effort=low` is ignored by the Qwen3.5 9B template), thinking tokens share the output budget; at 300 the model was cut off after thinking without producing a visible answer and the worker emitted "The model produced no answer text. The thinking and the findings obtained were kept on screen." The value now matches the real UI (`mamidlabs-desktop.ps1`: 8192) and the server's `--predict 8192`; this was a **test-run mismatch**, not an app bug (the `ui_chat` step passes with the real window). | scripts/smoke-test.ps1, AGENTS.md |
| 2026-10-12 | **The audio-transcription tier is now determined by the SELECTED language-model profile.** The local ASR pipeline's level used to be chosen only from total VRAM; now the **selected profile** decides: **lite** → `int8_float16`, sequential loading, 15 s CPU/GPU threshold; **pro** (MAX/FLASH) → `float16`, parallel, 20 s. Total VRAM is only a fallback metric when no profile name is given (CLI); the device/swap decision is still made from the measured real free VRAM + system RAM and the recording's duration. `config.py`: tier names `lite`/`pro`, `TIER_ALIASES` / `resolve_tier` / `select_profile_for`; `mamidlabs-inference.ps1`: new `Get-AsrProfileName`, a `profile` field on the plan/transcribe requests. | agent/transcriber/config.py, agent/whisper-tools.py, agent/transcriber/main.py, agent/mamidlabs-inference.ps1, AGENTS.md, docs/technical-notes.md, CHANGELOG.md |
| 2026-10-12 | **Added a language profile (download + delete) to the Profiles window:** the user picks the UI language (Turkish/English) and downloads and installs the other language edition's setup package right there (into the same folder, `UsePreviousAppDir`; `settings\`/`chats\` kept) or **deletes** the downloaded package. The downloaded package is stored persistently in `tools\editions\MamidLabs_Setup_<lang>.exe` with an `edition.json` record; "Delete language edition" removes the package + record. `agent/profile-catalog.ps1`: `MamidLabsEditionLang`, `OtherEditionRepo`, `Get-EditionRelease`, `Get-EditionDownloadDir`/`Get-EditionRecord`, a language ComboBox plus "Download selected language edition" and "Delete language edition" buttons. | agent/profile-catalog.ps1, AGENTS.md, CHANGELOG.md |
| 2026-10-12 | **Measured the CPU/GPU threshold with AR-GE; LITE now shares the unified decision.** Measurement (RTX 5060 Ti 16 GB, faster-whisper large-v3): CPU `~5+0.91·d`, GPU `~3.3+0.079·d` + swap (PRO 14 s, LITE 9 s). Break-even PRO ~15 s (-> threshold **20 s**), LITE ~9 s swap + 6 s load (-> threshold **15 s**). Both profiles now use the same `choose_plan`: fits alongside the model -> GPU/no swap; does not fit + enough RAM + SHORT recording -> CPU/no swap; long recording or insufficient RAM -> GPU + mandatory swap. LITE's CPU branch was re-enabled (measured: CPU path ~4.1 GB RAM, fits in 16 GB; on 8 GB the audio pipeline (~3.4 GB) does not fit alongside the language model, so a swap is still required). | agent/transcriber/config.py, tests/test_transcriber.py, AGENTS.md, CHANGELOG.md |
| 2026-10-12 | **FLASH output cap 4096 -> 8192:** all three profiles now use 8192 for `--predict` and the UI `max_tokens` (a ceiling, not a target; generation stops at EOS so normal answer speed is unchanged). The MAX/FLASH distinction is carried by `--reasoning-effort`. | scripts/manage-mamidlabs.ps1, mamidlabs-desktop.ps1, AGENTS.md, docs/technical-notes.md, CHANGELOG.md |
| 2026-10-12 | **Audio transcription engine moved from Qwen3-ASR to the `agent/transcriber/` pipeline (faster-whisper + AST); Qwen3-ASR and whisper.cpp were fully removed.** New package: `config` (tier from the SELECTED profile: lite int8_float16 + sequential, pro float16 + parallel; total GPU VRAM is only a fallback when no name is given), `audio` (PyAV 16 kHz mono WAV), `vad` (Silero), `asr` (**faster-whisper `Systran/faster-whisper-large-v3`**, hallucination controls: VAD, `condition_on_previous_text=False`, `no_speech/logprob/compression` thresholds, low-temperature fallback, known-phrase/repeat filtering), `events` (AST AudioSet -> readable phrases; "Speech" labels filtered), `diarize` (pyannote, optional), `merge` (**chronological, timestamp-free**: `(footsteps)` / `Speaker: ...`), `main` (txt+json). The installer creates **`venv\asr`** (faster-whisper + transformers/AST + pyannote + torch cu130) and prefetches models into the HF cache. Audio events default **on**; the microphone menu gained a **"Non-speech sounds"** checkbox (off -> speech only; `settings\api.json -> audioEvents`). The red microphone button now **cancels** the recording (stop+delete; sending is via the right-click menu/Send). Both language editions (TR: tr-Turkish tuned / ENG: auto + English labels). | agent/transcriber/*, agent/whisper-tools.py, agent/mamidlabs-inference.ps1, mamidlabs-desktop.ps1, scripts/install-mamidlabs.ps1, requirements/asr.txt, agent/profile-catalog.ps1, packaging/windows/mamidlabs.iss, tests/test_transcriber.py, AGENTS.md, CHANGELOG.md |
| 2026-10-12 | **Fixed an audio-transcript parsing bug (found in the UI test).** On a silent/short recording the Qwen3-ASR output parser (`_extract_text`) mistook the raw `language None<asr_text>` text for the transcript and sent it to the model. The `<asr_text>` tag and the `language ...` wrapper are now ALWAYS stripped; a silent result returns an **empty** text and the UI/worker continues without a transcript ("speech recognition produced no text"). Verified with a real silent recording + a TTS speech recording. | agent/qwen3-asr-tools.py, tests/test_qwen3_asr.py, CHANGELOG.md |
| 2026-10-11 | **LITE output cap raised 3072 → 8192 (`--predict`).** Because thinking is ON and **unlimited**, thinking tokens share the `--predict` **output** budget (a different thing from the 32768 context window). At 3072 even a simple request could be consumed entirely by thinking and cut off **with no visible answer** ("empty answer"). The UI already sends `max_tokens=8192` for LITE, so the server now matches. The smoke chat probe also uses 8192. | scripts/manage-mamidlabs.ps1, scripts/smoke-test.ps1, AGENTS.md, docs/technical-notes.md |
| 2026-10-11 | **"Stop" now cancels EVERYTHING running at that moment instantly** (the answer, long-running Python subprocesses such as ASR/OCR/search, media generation); the language model is then loaded in the background and chat continues on the next message. `Invoke-PythonTool` now collects output **asynchronously** and polls `$Shared.Cancel` in the wait loop; on cancel the subprocess tree is killed **immediately** (`$p.Kill($true)`), the ASR device loop breaks without the CPU fallback, and tool/ASR cancels are not shown as errors. **The one exception: a language-model LOAD is not cut** — `Cancel-Generation` does not touch `switchProcess`/`pendingModelChange`; the load completes in the background. | agent/mamidlabs-inference.ps1, mamidlabs-desktop.ps1, tests/test_qwen3_asr.py |
| 2026-10-11 | **Fixed the model-restore race after audio/OCR (8 GB LITE).** After the ASR/PaddleOCR subprocess exits, WDDM may release its VRAM late, so a language model started immediately could be rejected for "not enough memory" and **stay down**. The memory gate in `manage-mamidlabs.ps1` is now retried a **bounded** number of times with short waits (~5 s; ComfyUI is closed on the first attempt, only the last attempt raises). Also, the smoke test's chat probe now uses a realistic `max_tokens` (the LITE output cap) for unlimited thinking; a small value could be consumed entirely by thinking and yield an "empty answer". | scripts/manage-mamidlabs.ps1, scripts/smoke-test.ps1, tests/test_memory_admission.py, tests/test_smoke_test_script.py |
| 2026-10-11 | **LITE no longer uses a thinking budget (unlimited).** `--reasoning-budget` is never passed; thinking stays on and the model thinks as long as it needs. The system prompt tells it to think deeply and not to cap its thinking time; the former 300-token budget was removed. | scripts/manage-mamidlabs.ps1, agent/mamidlabs-inference.ps1, mamidlabs-desktop.ps1, docs/technical-notes.md, tests/test_lite_profile.py, scripts/smoke-test.ps1, README.md, CHANGELOG.md, AGENTS.md |
| 2026-10-11 | **Audio (microphone) flow fixed and ASR priority added.** (1) While recording, pressing the **red microphone button** **finishes and sends** the recording; when idle it opens the menu. The menu's **"Cancel"** now really **cancels** the recording (the file is deleted, nothing is sent); to reach it while recording, **right-clicking** the microphone button opens the menu and the labels become "Finish and send"/"Cancel recording" (`Stop-MicrophoneCapture`/`Complete-MicrophoneCapture`/`Cancel-MicrophoneCapture`). (2) Pressing **Send** while recording also finishes and sends the recording. (3) **ASR preempts the language-model load:** a turn with an audio attachment starts immediately even if the model is not ready; `Stop-ModelSwitchForAudio` kills the in-flight load, the worker frees VRAM, transcribes, then restores the model (per the **selected** profile when the active alias is absent, so the wrong profile is never started). (4) **"Stop" also works during an audio turn:** transcription and the model-restore wait honor the cancel flag (`Wait-LocalModelReady` does not wait when cancelled; a cancel is not shown as an error). | mamidlabs-desktop.ps1, agent/mamidlabs-inference.ps1, tests/test_qwen3_asr.py, tests/test_desktop_behavior.py, tests/test_lite_profile.py |
| 2026-10-11 | Fresh-install fix: added **`transformers==5.17.0`** to `requirements\imagegen.txt`. Qwen3-ASR needs `transformers>=5.13`; ComfyUI's requirement is only `>=4.50.3`, so the version was unpinned and a fresh install could get an older version and break **audio transcription**. | requirements/imagegen.txt, tests/test_requirements_files.py |
| 2026-10-11 | Install components are now **optional**: the wizard's **image/video generation, audio capture and OCR** tasks (all **checked by default**) skip the install when unchecked (`-SkipMedia`/`-SkipAudio`; OCR was already optional). Fixed the OCR size text to the real value (**~5.5 GB**). Also added **selecting and deleting** components from the Profile Manager (`Get-ProfileFileComponent`: ComfyUI->media, `models\qwen3-asr`->audio; `Remove-ProfileComponents` deletes only profile-unique files). | packaging/windows/mamidlabs.iss, scripts/install-mamidlabs.ps1, scripts/setup-run.ps1, agent/profile-catalog.ps1, tests/test_setup_wizard.py, tests/test_profile_delete.py |
| 2026-10-11 | Added an **"Uncensored / Open-Source AI Models and Third-Party Components"** clause to the EULA: filtered/unfiltered open-source models are provided in the service of science/education; the Owner does not create/train/own them and merely facilitates access to publicly available sources; the user is solely responsible for malicious/unlawful use and the Owner cannot be held liable. | EULA.txt, tests/test_setup_wizard.py |
| 2026-10-11 | Added an **EULA** (End User License Agreement) to the setup wizard: ownership/intellectual property, scope of the license, prohibited actions (copying/modifying/cracking/commercial marketing), legal consequences of unauthorized use, disclaimer and limitation of liability, termination, governing law and jurisdiction. `EULA.txt` (TR/ENG, UTF-8 BOM) is shown on the wizard's license page (`LicenseFile`) and bundled into the package. | EULA.txt, packaging/windows/mamidlabs.iss, tests/test_setup_wizard.py, .github/workflows/installer.yml |
| 2026-10-11 | **The audio transcription engine moved from whisper.cpp to Qwen3-ASR-1.7B** (whisper fully removed). New worker `agent/qwen3-asr-tools.py` (imagegen venv: torch cu130 + transformers 5.x; PyAV decodes to 16 kHz mono float32 numpy — no librosa/soundfile; `HF_HUB_OFFLINE`/`TRANSFORMERS_OFFLINE` for offline). 52 languages + **Turkish**, speech + singing/BGM; verified with real audio (LibriSpeech: exact match, on GPU). Installer stage `whisper` → **`asr`** (model downloaded to `models\qwen3-asr` from HF with size verification); `verify` check and UI text updated. `Invoke-TranscribeWithModelSwap` now tries `cuda→cpu`; the microphone-menu quality toggle (audioQuality) was removed (single engine). Test file `tests/test_whisper_tools.py` → `tests/test_qwen3_asr.py`. | agent/qwen3-asr-tools.py, agent/mamidlabs-inference.ps1, mamidlabs-desktop.ps1, scripts/install-mamidlabs.ps1, tests/test_qwen3_asr.py, tests/test_installer_pinning.py |
| 2026-10-11 | Re-verified the 1.0.0 release gate **after** the audio/microphone changes: **MAX smoke 14/14**, **LITE smoke 13/13** (+1 expected skip: `edit`, unsupported on 8 GB), pytest **TR 332 / ENG 331**, setup built for TR/ENG (`MamidLabs_Setup.exe`). Measured: MAX 43.1→47.7 tok/s (post-media ratio 1.11), LITE 95.8→92.9 tok/s (ratio 0.97), ComfyUI closed on both; UI turn MTP 23/23. | AGENTS.md |
| 2026-10-11 | Fixed a whisper.cpp install bug: because the archive extracts into a `Release` subfolder the "whisper-cli.exe already installed" skip never matched, so **662 MB of binaries redownloaded on every install**; `whisper-cli.exe` is now found recursively. | scripts/install-mamidlabs.ps1, tests/test_installer_pinning.py |
| 2026-10-11 | Audio capture quality is now selectable: a **"High quality (large-v3)"** checkbox (toggle) in the microphone menu. Unchecked = normal (turbo-q5_0, fast), checked = high (large-v3, highest accuracy, ~2x slower). The choice is stored in `settings\api.json` → `audioQuality` and the worker passes the explicit model path to whisper. The installer now downloads **both** models (turbo-q5_0 + large-v3 + VAD); otherwise high quality silently fell back to turbo on a fresh install. | mamidlabs-desktop.ps1, agent/mamidlabs-inference.ps1, scripts/install-mamidlabs.ps1, tests/test_whisper_tools.py |
| 2026-10-11 | Added dependency-free microphone recording: winmm `mciSendString` records the Windows **default** input device (WAVE_MAPPER); the microphone button toggles start/stop. The recording is attached as audio and sent automatically (transcript + answer). | mamidlabs-desktop.ps1, tests/test_whisper_tools.py |
| 2026-10-11 | Made the ComfyUI lifecycle **profile-aware**: on MAX/FLASH (27B, at the card's limit on 16 GB) an idle ComfyUI is **always closed** before loading — otherwise the MTP draft context moves to the CPU and chat drops **intermittently to ~6-7 tok/s** (measured: the post-media switch step once gave a 0.18 ratio). LITE (9B) runs with ComfyUI (ratio 1.04) and **keeps it open**. This removes the "ComfyUI open + 27B" flakiness. | scripts/manage-mamidlabs.ps1, tests/test_memory_admission.py |
| 2026-10-11 | Added a smoke test `ui_chat` step (drives the real window like a user: type + Send, a response must arrive); fixed a `$pid` automatic-variable clash. | scripts/smoke-test.ps1, tests/test_smoke_test_script.py |
| 2026-10-11 | Added **chemistry/general-science** glyphs to the math renderer (`Convert-MathText`): equilibrium arrow ⇌ plus ⇋ ⇄ ⇆ ↔ ⟷ ⟹ ↪ ↑ ↓ ↕ and **mhchem `\ce{...}`/`\pu{...}`** support (content made readable: element numbers become subscripts `H2O`→H₂O, arrows `->`/`<=>`→→/⇌). `->`/`<-` are converted only inside `\ce`; `->` in code blocks is not corrupted. | mamidlabs-desktop.ps1, tests/test_math_render.py |
| 2026-10-11 | Added audio transcription (whisper.cpp): attached audio is transcribed automatically and the findings are given to the language model (OCR/mmproj pattern). `agent/whisper-tools.py` converts to 16 kHz mono WAV and runs `whisper-cli`; **turbo-q5_0 + Silero VAD** (measured: 2 min audio 53 s CPU / 2.5 s GPU; VAD removes hallucination). `Invoke-TranscribeWithModelSwap` unloads the local model (leaving the GPU to whisper) and restores it, like OCR. UI: a **microphone** button left of Send (Use microphone), **Add audio** in the "+" menu, and a right-click **Paste** (media/text) menu in the prompt box. Also **passed `custom` baseUrl to the worker** (the base address was empty in the worker → the custom provider did not work in chat; found in a real end-to-end test, fixed). | agent/whisper-tools.py, agent/mamidlabs-inference.ps1, mamidlabs-desktop.ps1, tests/test_whisper_tools.py, tests/test_mamidlabs_contracts.py |
| 2026-10-11 | Provider **maximum context auto-detection**: when the key is verified, `context_length` is read from the model list endpoint and the context window is applied automatically (commandcode: 1,000,000). `Get-ProviderModelContexts` (api-providers.ps1) + api-settings integration. API effort is default; `custom` verified end to end (text + image). | agent/api-providers.ps1, agent/api-settings.ps1, tests/test_api_providers.py |
| 2026-10-10 | 1.0.0 pre-release variant gate + verification: **PRO (flash) smoke 13/13**, **LITE smoke 12/13** (+1 expected skip: `identity_edit`), pytest **TR 318 / ENG 317**, install `verify` 12/12, Inno package built for TR/ENG and **v1.0.0 released**. LITE measurement: 85.7 tok/s, post-media 88.4 (ratio 1.03), ComfyUI stayed open. Added a "pre-release variant gate" to section 15. | AGENTS.md |
| 2026-10-10 | Solved "ComfyUI must stay open": leak/duplicate detection uses instance **roots**, not process **count**. ComfyUI is a process pair (launcher + base-python child), so `Count -gt 1` mistook the single healthy ComfyUI for a leak and closed it (the opposite of the goal). `Get-ComfyInstanceRoots` + `Stop-ComfyLeaks` close only extra roots and keep the instance on :8188 (`Test-ComfyUnderRoot`); `media-model-swap.ps1` `Clear-ComfyLeaks` mirrors it; the old `Select-Object -First 1` closed only one half. Measured: after a real image job MAX loaded WITHOUT closing ComfyUI -> **62 tok/s**. | scripts/manage-mamidlabs.ps1, agent/media-model-swap.ps1, tests/test_memory_admission.py |
| 2026-10-10 | Fully verified: `python -m pytest -q` **318 passed** (4 expected skips) and the full smoke test **13/13** (chat 44 tok/s, MTP acceptance 82/87, chart bar/pie/function, image 56 s, edit 96 s, post-media switch ratio 0.97, video 161 s, UI turn MTP 23/23). The installed root is **byte-identical to the repo in the program files**. | AGENTS.md |
| 2026-10-10 | Added dependency-free chart generation: the chat tool `generate_chart` + `agent/chart-run.ps1` (bundled pwsh + System.Drawing; bar/line/pie/scatter/function; a safe expression evaluator, no `Invoke-Expression`). The result is shown inline in the chat and saved under `outputs\charts`. Added a `chart` step to the smoke test (`-SkipChart`). Applied to the Turkish edition too. | mamidlabs-desktop.ps1, agent/mamidlabs-inference.ps1, agent/chart-run.ps1, scripts/smoke-test.ps1, tests/test_chart_run.py |
| 2026-10-10 | Added `Convert-MathText` for LaTeX math answers: instead of raw `$$`/`\frac`/`\int`, readable Unicode (∫, ∞, π, √, ∑, →, ≠, Greek letters), fractions → a/b, exponents/subscripts → Unicode (x², ∫₀). A currency `$` is kept. | mamidlabs-desktop.ps1, tests/test_math_render.py |
| 2026-10-09 | Added the `comfyKeepAlive` setting (default `true`): ComfyUI stays open between media jobs (no reopen). Measured: no spill (identical buffer layout, graph splits 2), chat ~11% slower (49.7→44.2 tok/s). `false` restores full speed. Also made two flaky tests deterministic (run_command output encoding UTF-8; empty-file test uses tmp_path). | scripts/manage-mamidlabs.ps1, tests/test_memory_admission.py, tests/test_bound_path_and_run_command.py, tests/test_binary_file_detection.py |
| 2026-10-09 | Hardened the single-instance check: it now verifies the window CLASS, not just the title. Another window with the same title (Explorer `TabProxyWindow`, a video window) made the app exit before opening ("window closed before opening"). | mamidlabs-desktop.ps1, tests/test_mamidlabs_contracts.py |
| 2026-10-09 | Fixed the structural root cause of the spill / "Model operation failed": the app's only big CUDA context, ComfyUI, is now ALWAYS closed before the language model loads (with the context open the driver fragments VRAM; 15,290 MiB "free" + context → 6 tok/s). The MAX/FLASH margin was lowered 800→600 MiB (threshold 15200); a 27B model that runs fine at 15244 MiB free on a clean GPU is no longer rejected. LITE+MAX verified together (97 / 58 tok/s). Added "Delete profile" to the Profile manager (only profile-unique files; LITE's shared media is kept). The 27B wizard label is now "PRO". Applied to the Turkish edition too. | scripts/manage-mamidlabs.ps1, agent/profile-catalog.ps1, packaging/windows/mamidlabs.iss, tests/test_memory_admission.py, tests/test_profile_delete.py |
| 2026-10-09 | Fixed a VRAM threshold regression: the earlier margin of 1000 MiB made the threshold 15600 MiB and wrongly rejected the machine's normal free value (15442–15558). The margin is now 800 MiB (threshold 15400); ComfyUI is closed when open, the model loads when it is closed / on a cold boot. Applied to the Turkish edition too. | scripts/manage-mamidlabs.ps1, tests/test_memory_admission.py |
| 2026-10-09 | Fixed the root cause of the post-media drop to ~6 tok/s: the VRAM budget under-counted the real load (margin 300→1000 MiB, MAX/FLASH tolerance 256→0). The 27B model needs ~15378-15442 MiB on the GPU; with an idle ComfyUI the MTP draft context moved to the CPU and production dropped 44→6-7 tok/s. LITE is unchanged (margin 300, tolerance 1024). `Release-Comfy` now verifies real free VRAM. Added a `ui` smoke step that drives the real app (MTP + "connection dropped" check). | scripts/manage-mamidlabs.ps1, agent/media-model-swap.ps1, agent/imagegen-run.ps1, agent/videogen-run.ps1, scripts/smoke-test.ps1, tests/test_memory_admission.py, tests/test_media_release.py, tests/test_smoke_test_script.py |
| 2026-10-09 | Script-based updating (`install-mamidlabs.ps1 -Stage layout`) now refreshes the wizard source package (`setup-source`) if present, so it no longer stays at the previous version. | scripts/install-mamidlabs.ps1, tests/test_install_layout_source.py |
| 2026-10-08 | Added local web management: browser access to chat/model switching/generated files. The server (`agent/web-server.ps1`) binds to localhost only by default, has a fixed password + HttpOnly/SameSite=Strict session cookie (30 days), a single-use 5-minute connect link (OpenCode 'pair' style), locks out after 5 failed logins, and confines file access to `outputs\`. Also: the VRAM check now tolerates a small overflow after MTP (fixes the model-does-not-start error); the private-repo update check uses a GitHub token; generated files open in one click via "Open". | agent/web-server.ps1, agent/web-settings.ps1, scripts/web-serve.ps1, scripts/manage-mamidlabs.ps1, agent/profile-catalog.ps1, mamidlabs-desktop.ps1, tests/test_web_server.py, tests/test_memory_admission.py, tests/test_mamidlabs_contracts.py |
| 2026-10-08 | The license was set to proprietary (all rights reserved; copyright: Aybars Haydar Altuntaş). | LICENSE, README.md, docs/installation.md, tests/test_setup_wizard.py, CHANGELOG.md |
| 2026-10-08 | Enabled MTP on LITE: the MTP/nextn head dropped in conversion is added to the 9B GGUF during download by `mtp_inject.py` (only the MTP tensors from the source HF safetensors via HTTP range; `w+1` on norms). `manage-mamidlabs.ps1` now enables MTP whenever the GGUF has `nextn` layers (max/flash/lite) via `Test-GgufHasMtp`. Measured acceptance ~0.93, ~2x speed; output is lossless. | agent/mtp_inject.py, agent/hf-hub.py, agent/profiles.json, agent/profile-catalog.ps1, scripts/download-profile.ps1, scripts/manage-mamidlabs.ps1, tests/test_lite_profile.py, tests/test_mtp_inject.py |
| 2026-10-08 | Enabled MTP (Multi-Token Prediction) speculative decoding (`--spec-type draft-mtp`); no separate draft model is needed and the output is lossless. Disable with `settings.mtp=false`. | scripts/manage-mamidlabs.ps1, tests/test_lite_profile.py |
| 2026-10-07 | `run_command` is now offered only when the user explicitly asks for a command/terminal (`Test-CommandIntent`); the model can no longer work around a web failure with commands. Added a LITE deep-reasoning line; after a web failure web tools are not offered and network-probe commands are refused. | agent/mamidlabs-inference.ps1, mamidlabs-desktop.ps1, tests/test_mamidlabs_contracts.py, tests/test_lite_profile.py, tests/test_approval_flow.py |
| 2026-10-07 | Added a `history_resume` step to the smoke test: save chat with DPAPI → reload → continue; no turn errors (skippable with `-SkipHistoryResume`). The step guards with `if ($done.data)` because $done.data comes back as $null on a tool-less turn. `test_chat_storage.py` verifies the save/load serialization. | scripts/smoke-test.ps1, tests/test_chat_storage.py, tests/test_smoke_test_script.py |
| 2026-10-07 | Added a `network_switch` step to the smoke test: across successive turns the network goes online/offline and the local worker must not error in any turn (skippable with `-SkipNetworkSwitch`). | scripts/smoke-test.ps1, scripts/setup-run.ps1, agent/mamidlabs-inference.ps1 |
| 2026-10-07 | Broadened the local-stream drop tolerance: when a usable result exists (finish_reason or answer text or a tool call), a dropped connection is not an error, so no "[Error]" line is added under the answer. | agent/mamidlabs-inference.ps1, tests/test_stream_watchdog.py |
| 2026-10-07 | Added an `offline` step to the smoke test: after installation, network access is cut to prove the web tool returns error JSON without crashing (skippable with `-SkipOffline`). | scripts/smoke-test.ps1, scripts/setup-run.ps1, tests/test_offline_mode.py |
| 2026-10-07 | Added offline behavior tests: web search, page reading and reference image tools return clean error JSON (no exception) when the network is down; the real tool subprocess behind a black-hole proxy fails cleanly. | tests/test_offline_mode.py, docs/technical-notes.md |
| 2026-10-07 | Fixed the intermittent "forcibly closed by the remote host" (WSAECONNRESET) error during local model streaming: local requests now use a fresh connection per turn and a drop after answer text is no longer treated as an error. | agent/mamidlabs-inference.ps1, tests/test_stream_watchdog.py, tests/test_mamidlabs_contracts.py, docs/technical-notes.md |
| 2026-09-27 | 1.0.0 first release. | all |
