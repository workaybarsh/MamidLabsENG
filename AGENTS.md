# MamidLabs · Infrastructure guide for developers and AI agents

This file is the single source of truth for developers and AI agents (Claude, Codex, Gemini,
Copilot…) working on MamidLabs. It explains how the codebase starts, how the parts talk to each
other, where things live and the known pitfalls.

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
editing and video generation. The language model runs on `llama.cpp` (`llama-server`), the
image/video models on `ComfyUI`, on the user's NVIDIA graphics card. OpenAI, Claude, Gemini,
DeepSeek and custom OpenAI-compatible endpoints are optional API providers.
Repository: `github.com/workaybarsh/MamidLabsENG`. Default install root:
`%LOCALAPPDATA%\Programs\MamidLabs`.

## 2. Repository map

| Path | Content |
|---|---|
| `qwen-desktop.ps1` | Main app: WinForms UI, chat state, starting jobs, event loop |
| `launch-qwen-desktop.vbs` | Target of the desktop shortcut; starts the app without a console |
| `agent/qwen-inference.ps1` | Chat worker: model request, streaming, tools (tool calling), approval, context compaction |
| `agent/api-providers.ps1` | Provider table, request/response converters (OpenAI-compatible ↔ Claude Messages) |
| `agent/api-settings.ps1` | API connections window, key verification, DPAPI key storage |
| `agent/media-settings.ps1` | Image and video models window, "Choose model file (file path)" |
| `agent/hf-settings.ps1`, `agent/hf-hub.py` | Hugging Face token and download worker (size-verified) |
| `agent/imagegen-run.ps1` | Local image generation / editing worker (ComfyUI API) |
| `agent/videogen-run.ps1` | Local video generation worker (Wan 2.2) |
| `agent/api-media-run.ps1`, `agent/api-media.py` | Image/video generation via API, profile download job |
| `agent/media-model-swap.ps1` | Unload the language model → start/reuse ComfyUI → reload (single owner) |
| `agent/media-paths.ps1` | External (not copied) model paths, `comfy-extra-models.yaml` |
| `agent/model-registry.ps1` | Model selector entries, profile aliases |
| `agent/profile-catalog.ps1`, `agent/profiles.json` | Profile file catalog, Profiles window, downloading missing files, update check |
| `agent/process-launch.ps1` | `Start-DetachedProcess`: start a child process without inheriting the stdout pipe |
| `agent/qwen-agent-tools.py` | Web search (ddgs), page reading, bound-folder tools |
| `agent/office-tools.py` | Reading PDF/Word/Excel/PowerPoint/ZIP/RAR, creating PDF/DOCX/PPTX |
| `agent/ocr-tools.py` | PaddleOCR worker (GPU image, CPU PDF) |
| `agent/media-tools.py` | Video frame extraction etc. (PyAV) |
| `agent/workflows/krea2_identity_edit.json` | Photo editing workflow (ComfyUI UI format) |
| `scripts/install-mamidlabs.ps1` | The actual installation (staged, pinned versions) |
| `scripts/setup-run.ps1` | The wizard's back end: prerequisites → installation → models → summary |
| `scripts/uninstall-mamidlabs.ps1` | Full uninstall (Windows PowerShell 5.1, ASCII) |
| `scripts/manage-qwen.ps1` | Start/stop/switch `llama-server` by profile, memory gate |
| `scripts/download-profile.ps1`, `profile-report.ps1`, `configure-models.ps1`, `install-ocr.ps1` | Profile download, hardware report, own model paths, OCR installation |
| `scripts/smoke-test.ps1` | End-to-end verification with the real models |
| `scripts/diagnose-latency.ps1` | VRAM, cache hit and speed diagnostics |
| `packaging/windows/mamidlabs.iss` | Inno Setup wizard; **the single source of the version** (`MyAppVersion`) |
| `requirements/app.txt`, `requirements/imagegen.txt` | Python packages (pinned versions) |
| `tests/` | pytest; also runs PowerShell functions with a real `pwsh` |
| `tests/ui-automation/` | Helpers that drive the app and the wizard with UI Automation/Win32 |
| `docs/` | `installation.md`, `technical-notes.md`, `media/` |
| `.github/workflows/` | `tests.yml` (pytest), `installer.yml` (Inno build and release publishing) |
| `AGENTS.md`, `CLAUDE.md` | This guide; `CLAUDE.md` points here for Claude Code. Not copied into the install root. |

## 3. Install root layout (`%LOCALAPPDATA%\Programs\MamidLabs`)

```
agent\ scripts\ assets\          program files (copied again from the source on every installation)
qwen-desktop.ps1 launch-qwen-desktop.vbs README.md LICENSE
runtime\pwsh\pwsh.exe            bundled PowerShell 7.6.6 (the app runs ONLY with this)
llama.cpp\b10998\llama-server.exe CUDA 13.4 build
venv\app\                        the app's Python (tools, HF, documents)
venv\imagegen\                   ComfyUI + torch cu130 + PyAV
venv\ocr\ venv\ocr-cpu\          PaddleOCR (optional, checked by default in the wizard; install-ocr.ps1)
ComfyUI\                         pinned commit; custom_nodes: ComfyUI-GGUF, comfyui-krea2edit
ComfyUI\models\{diffusion_models,unet,text_encoders,vae,loras}\
models\                          language models (.gguf + mmproj)
settings\api.json                all user settings (+ encrypted API keys)
settings\comfy-extra-models.yaml external model folders used without copying
settings\install-record.json     packages the wizard installed with winget (read by the uninstaller)
chats\                           chats (encrypted with DPAPI)
outputs\{images,videos,documents,backups,refs,thumbs,input,comfy}\
logs\                            qwen-<profile>.std{out,err}.log, comfyui.*.log, audit.jsonl,
                                 setup.log, setup-summary.txt, setup-step.txt, smoke-test-last.json
tools\pip-cache\ tools\hf-home\  pip and Hugging Face caches (not written to the user profile)
tools\downloads\ tools\paddle-cache\ tools\backup\
setup-source\                    the source package extracted by the setup wizard
```

## 4. Launch chain

1. Desktop `MamidLabs.lnk` → `launch-qwen-desktop.vbs` (finds the root from its own location).
2. VBS: `runtime\pwsh\pwsh.exe -NoProfile -ExecutionPolicy Bypass -STA -WindowStyle Hidden -File qwen-desktop.ps1`.
3. `qwen-desktop.ps1` first checks whether `Start-ThreadJob` exists. If it was opened with the wrong
   host (Windows PowerShell 5.1) it relaunches itself with the bundled pwsh.
4. It dot-sources the modules (`api-providers`, `model-registry`, `profile-catalog`, `api-settings`,
   `media-settings`, `hf-settings`, `media-paths`…), reads `settings\api.json` and builds the form.
5. `manage-qwen.ps1 -Action switch -Profile <p>` is called for the selected profile; a timer polls
   `http://127.0.0.1:8080/health`. When it is ready the status becomes "Ready".
6. When the app closes (`FormClosing` → `Shutdown-All`) the chat is saved, and `llama-server` and an
   idle ComfyUI are closed too (VRAM is freed).

## 5. Processes and ports

| Process | Port | Owner | Note |
|---|---|---|---|
| `llama-server.exe` | 8080 | `scripts/manage-qwen.ps1` | Mutex `Local\QwenLocalModelControl`; the profile is recognized via `--alias` |
| ComfyUI (`venv\imagegen\Scripts\python.exe ComfyUI\main.py`) | 8188 | `agent/media-model-swap.ps1` | Stays open after a job, only unloads the models |
| App | — | `pwsh.exe … qwen-desktop.ps1` | Workers are `Start-ThreadJob` runspaces in the same process |

**Profiles** (`manage-qwen.ps1`; the aliases must match `model-registry.ps1` exactly):

| Profile | Alias | Model | Important flags |
|---|---|---|---|
| MAX | `qwen3.8-27b-max` | Qwen3.8 27B IQ4_XS + mmproj F16 | `--no-mmproj-offload` (projector on the CPU), ctx 32768, predict 8192, `--reasoning-effort xhigh`, q4_0 KV, batch 512/256, `--spec-type draft-mtp` |
| FLASH | `qwen3.8-27b-flash` | same files | predict 4096, `--reasoning-effort low`, `--spec-type draft-mtp` |
| LITE | `qwen3.5-9b-lite` | Huihui-Qwen3.5 9B Q5_K_M + mmproj Q8_0 | ctx 32768, predict 3072, `--reasoning-budget 300`, batch 256/128, projector on the GPU (no MTP) |

**MTP (Multi-Token Prediction).** The Qwen3.8 27B GGUF carries a built-in MTP/nextn draft head
(`qwen35.nextn_predict_layers = 1`, `blk.64.nextn.*`); so `--spec-type draft-mtp` needs **no separate
draft model** and is lossless (accepted tokens are verified against the target model). The model card
measures acceptance 0.35–0.53 and mean accepted length 2.05–2.58 on Q4_K_M, verified compatible with
mmproj. It is added automatically for MAX/FLASH; the LITE model has **no** MTP layers (llama.cpp
returns "model doesn't contain MTP layers"), so it is not added there. Disable with
`settings/api.json` → `"mtp": false`. When running without MTP the server logs `unused tensor
blk.64.*` warnings (expected).

Since MAX and FLASH use the same files, the setup wizard and the Profiles window show them as a
single "Qwen3.8 27B" entry (`Get-ProfileCatalogForDisplay`); switching happens in the model bar.
Lookups by id (`Get-ProfileEntry`, `Get-ProfileState`, `Test-LocalProfileChatFilesPresent`) must use
the full catalog (`Get-ProfileCatalog`).

**VRAM check** (`Test-MemoryBudget`): required = model file (+ mmproj on LITE) + 300 MiB. If free
VRAM is not enough, an idle ComfyUI is closed first and memory is measured again; if it is still not
enough a "Not enough free VRAM" error is raised. The goal is to keep the model from spilling into
shared system memory and slowing chat down. On a 16 GB card the 27B model uses ~14,640 MiB.

## 6. Flow of a chat request

1. The user sends → `Begin-Generation` (`qwen-desktop.ps1`).
2. `Build-JobMessages`: fixed system prompt (`Get-SystemPrompt`) + history. Image data from older
   turns is dropped and a note is put in its place. Per-turn hints (`Get-TurnHints`) are added **to
   the start of the last user message** as a `[System note]`, not to the system prompt, so
   llama-server's KV cache prefix is not broken.
3. `Start-ThreadJob -FilePath agent\qwen-inference.ps1 -ArgumentList $jobData, $sharedRequest`.
   `$jobData.root` carries the root **explicitly** (see pitfall 2).
4. The worker produces `[pscustomobject]@{kind; text; usage; data}` events with `Emit`. The UI timer
   reads them. Kinds: `thinking`, `answer`, `answer-rewrite`, `status`, `metrics`, `tool`,
   `artifact`, `ocr`, `imagegen`, `videogen`, `compact`, `denied`, `cancelled`, `error`, `done`.
5. **Tools** (`qwen-inference.ps1`): `search_web`, `fetch_page`, `read_document`,
   `read_images_ocr`, `create_document`, `generate_image`, `generate_video`, `find_reference_image`,
   `ask_model`, `list_bound_files`, `read_bound_file`, `write_bound_file`, `run_command`. Tools are
   defined based on intent and the chips (Internet, Agent).
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
- **Modes** (`generate_image.mode`):
  - `new`: generates from text. Krea-2 Turbo, 8 steps, cfg 1.
  - `regenerate` / `style_reference`: start from the attached image.
  - `identity_edit`: works with the Krea 2 Identity Edit v1.2 LoRA
    (`krea2_identity_edit_v1_2.safetensors`) and the `comfyui-krea2edit` nodes; the person's face is
    preserved. **MAX/FLASH (16 GB+) only**; not offered on LITE (see section 13), where photo
    editing requests are routed to `regenerate` (`agent/qwen-inference.ps1`
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
  `qwen-desktop.ps1`, `Invoke-AskModel` and the main request loop in `qwen-inference.ps1`;
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

## 10. Installation, wizard, updates and uninstall

- `install-mamidlabs.ps1 -Profile <p> [-Root] [-Stage a,b] [-SkipMedia] [-Force] [-NoUninstallEntry]`.
  Stages in order: `preflight`, `layout` (program files, shortcut, Windows uninstall entry),
  `runtime`, `llama`, `python`, `comfyui` (brings the pinned commits, backs up local changes to
  `tools\backup\*.patch`), `verify`. If the app is open, `layout` stops because the files are
  locked. Package sizes are verified. The `-Root` default is `%LOCALAPPDATA%\Programs\MamidLabs`.
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
  version in Inno's uninstall key (`{AppId}_is1\DisplayVersion`). Three states: idle → check → if an
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
  - Deletes the known entries in the root, the `MamidLabs*.lnk` shortcuts pointing to this root, the
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
  - LITE settings: `liteModel`, `liteContext`, `liteReasoningBudget`.
- Chats are encrypted under `chats\` with the `MAMIDLABS-CHAT-DPAPI-V2` prefix.
- `logs\audit.jsonl`: a record of tool calls (not content; path/command identity).

## 12. Tests

```powershell
python -m pip install pytest -r requirements\app.txt
python -m pytest -q          # ~250 tests, ~90 s; no extra setup needed (tests/conftest.py)
%LOCALAPPDATA%\Programs\MamidLabs\runtime\pwsh\pwsh.exe -NoProfile -ExecutionPolicy Bypass -File %LOCALAPPDATA%\Programs\MamidLabs\scripts\smoke-test.ps1 -Profile flash -SkipVideo
```

- **`conftest.py`:** if `pwsh` is not on PATH it adds the bundled pwsh of an installed MamidLabs
  (`MAMIDLABS_ROOT` or the default root) to PATH and adds the `venv\app` packages to `sys.path`.
  Many tests extract PowerShell functions via AST and run them in a real pwsh. Without PowerShell 7
  these tests are skipped; a skipped test does not count as a pass.
- `test_uninstall.py` really deletes in a fake root; it uses Windows PowerShell 5.1.
- **Smoke test steps:** `chat`, `document`, `offline` (the web tool returns error JSON without
  crashing while offline), `network_switch` (no turn errors while the network goes online/offline),
  `history_resume` (save chat → reload → continue; no errors), `ocr`, `image`, `edit`,
  `post_media_switch` (the speed
  ratio after an image must be ≥ 0.7), `video`.
- **CI:** `tests.yml` (windows-latest, Python 3.12) on every push/PR; `installer.yml` when
  package-related paths change. Merged into `master` via PR.

## 13. Hardware facts (measured)

| | 16 GB (RTX 5060 Ti) FLASH | 8 GB (RTX 4060 Laptop) LITE |
|---|---|---|
| Chat | ~24–25 tok/s | ~36 tok/s (on AC), 17–24 on battery |
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

## 14. Critical pitfalls

1. **The app only runs with the bundled PowerShell 7.** `Start-ThreadJob` does not exist in 5.1. If
   it is opened with the wrong host it silently stays at "Could not start the request";
   `qwen-desktop.ps1` therefore relaunches itself.
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
   `manage-qwen | …` hangs.
7. **The VRAM margin is kept small** (300 MiB). A large margin keeps MAX from ever starting on a 16 GB
   card. On MAX/FLASH the mmproj runs on the CPU (`--no-mmproj-offload`). On the GPU it spills
   ~420 MiB into shared memory and speed drops from 25 to 19 tok/s.
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
    the `layout` stage with "MamidLabs is open" when it sees a running `qwen-desktop.ps1`. That is why
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

## 15. Checklist when making changes

- [ ] Run the relevant tests; add a test for new behavior (`python -m pytest -q`).
- [ ] If you touched the media/chat flow, run the smoke test with the relevant steps.
- [ ] Is user-facing text in English and saved with the right encoding (BOM)?
- [ ] Install/uninstall impact: does a new file or folder write outside the root? If so, add it to
      the uninstaller. By default everything must stay under the root.
- [ ] Are `README.md`, `docs/installation.md`, `docs/technical-notes.md`, `CHANGELOG.md` and **this
      file** up to date?
- [ ] Does the Turkish edition need the same change? Identifiers there are Turkish.
- [ ] If releasing: do the `.iss` `MyAppVersion`, the CHANGELOG section and the `v<version>` tag match?
- [ ] To update an installed copy, close the app, then run `install-mamidlabs.ps1 -Stage layout`.

## 16. Change log

Add one line per feature/change (newest first).

| Date | Change | Files |
|---|---|---|
| 2026-10-08 | Enabled MTP (Multi-Token Prediction) speculative decoding for MAX/FLASH (`--spec-type draft-mtp`); the Qwen3.8 27B GGUF has a built-in MTP head so no separate draft model is needed; LITE has no MTP layers. Disable with `settings.mtp=false`. | scripts/manage-qwen.ps1, tests/test_lite_profile.py |
| 2026-10-07 | `run_command` is now offered only when the user explicitly asks for a command/terminal (`Test-CommandIntent`); the model can no longer work around a web failure with commands. Added a LITE deep-reasoning line; after a web failure web tools are not offered and network-probe commands are refused. | agent/qwen-inference.ps1, qwen-desktop.ps1, tests/test_qwen_contracts.py, tests/test_lite_profile.py, tests/test_approval_flow.py |
| 2026-10-07 | Added a `history_resume` step to the smoke test: save chat with DPAPI → reload → continue; no turn errors (skippable with `-SkipHistoryResume`). The step guards with `if ($done.data)` because $done.data comes back as $null on a tool-less turn. `test_chat_storage.py` verifies the save/load serialization. | scripts/smoke-test.ps1, tests/test_chat_storage.py, tests/test_smoke_test_script.py |
| 2026-10-07 | Added a `network_switch` step to the smoke test: across successive turns the network goes online/offline and the local worker must not error in any turn (skippable with `-SkipNetworkSwitch`). | scripts/smoke-test.ps1, scripts/setup-run.ps1, agent/qwen-inference.ps1 |
| 2026-10-07 | Broadened the local-stream drop tolerance: when a usable result exists (finish_reason or answer text or a tool call), a dropped connection is not an error, so no "[Error]" line is added under the answer. | agent/qwen-inference.ps1, tests/test_stream_watchdog.py |
| 2026-10-07 | Added an `offline` step to the smoke test: after installation, network access is cut to prove the web tool returns error JSON without crashing (skippable with `-SkipOffline`). | scripts/smoke-test.ps1, scripts/setup-run.ps1, tests/test_offline_mode.py |
| 2026-10-07 | Added offline behavior tests: web search, page reading and reference image tools return clean error JSON (no exception) when the network is down; the real tool subprocess behind a black-hole proxy fails cleanly. | tests/test_offline_mode.py, docs/technical-notes.md |
| 2026-10-07 | Fixed the intermittent "forcibly closed by the remote host" (WSAECONNRESET) error during local model streaming: local requests now use a fresh connection per turn and a drop after answer text is no longer treated as an error. | agent/qwen-inference.ps1, tests/test_stream_watchdog.py, tests/test_qwen_contracts.py, docs/technical-notes.md |
| 2026-09-27 | 1.0.0 first release. | all |
