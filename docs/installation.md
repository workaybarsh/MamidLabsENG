# Installation

This document describes the first installation of MamidLabs, updating it, verifying it and fixing
common problems.

## Requirements

| | LITE | MAX / FLASH |
|---|---|---|
| Operating system | Windows 10 2004+ or Windows 11 | Windows 10 2004+ or Windows 11 |
| Graphics card | NVIDIA, at least 8 GB VRAM | NVIDIA, at least 16 GB VRAM |
| Memory | 16 GB RAM | 32 GB RAM |
| Free disk (SSD) | ~58 GB (models ~36 GB) | ~75 GB (models ~52 GB) |
| Software | A current NVIDIA driver | A current NVIDIA driver |

The install script checks these conditions in its preflight and reports anything missing as a
warning. If Python 3.10+, Git, 7-Zip or the Visual C++ Redistributable are missing, the wizard
installs them with winget; for a script installation Python and Git must already be installed.

## One-click installation (wizard)

Running `MamidLabs_Setup.exe` brings up these pages:

| Page | Job |
|---|---|
| Welcome and license | MIT license |
| Install folder | Default is the standard per-user program folder (`%LOCALAPPDATA%\Programs\MamidLabs`); can be changed. 40-80 GB including the models, an SSD is recommended |
| Profile report | GPU, VRAM, RAM and free disk are measured; a suitable / borderline / not suitable verdict and a recommendation for each profile (`scripts\profile-report.ps1`) |
| Model profile | LITE, Qwen3.8 27B (covers both the FLASH and MAX runtime presets — switchable from the app's model bar after install), **do not download now (I will install later)** or **my own model / my API account** |
| Your own models | Choose the language (.gguf), image (Krea 2 Turbo) and video (Wan 2.2 TI2V 5B) files by path; they are not copied |
| API account | Open the API connections window on first launch (no key is asked for during setup) |
| Additional tasks | Smoke test at the end of setup, OCR components |

After the Install button, `scripts\setup-run.ps1` runs in the background and reports its progress
to the wizard: it installs missing prerequisites with winget, runs `install-mamidlabs.ps1`, downloads the chosen profile's
models (`download-profile.ps1`), writes the model preferences (`configure-models.ps1`), runs the
smoke test and writes the `logs\setup-summary.txt` summary. The wizard's last page shows this
summary; the full output is in `logs\setup.log`. All files of the three profiles (including the
27B language model and the Identity Edit LoRA) are downloaded from Hugging Face with pinned versions
and size verification.

To build the wizard: GitHub Actions → **Installer package** (or locally with Inno Setup 6:
`iscc packaging\windows\mamidlabs.iss`). The output is `dist\MamidLabs_Setup.exe`.

To **install the profile later**, use Profiles → Profile manager → Download missing in the app, or:

```powershell
%LOCALAPPDATA%\Programs\MamidLabs\runtime\pwsh\pwsh.exe -File %LOCALAPPDATA%\Programs\MamidLabs\scripts\download-profile.ps1 -Profile lite
```

To **add your own model later**, use "Add local language model (GGUF)…" and
"Image and video models → Choose model file (file path)…" in the app, or:

```powershell
%LOCALAPPDATA%\Programs\MamidLabs\runtime\pwsh\pwsh.exe -File %LOCALAPPDATA%\Programs\MamidLabs\scripts\configure-models.ps1 -LlmPath D:\models\model.gguf -ImageModelPath D:\models\krea2_turbo_fp8_scaled.safetensors
```

## Installation with the script

1. Download the source:
   ```powershell
   git clone https://github.com/workaybarsh/MamidLabsENG.git C:\MamidLabs-src
   ```
2. Run the install script with your profile (`lite`, `flash` or `max`):
   ```powershell
   powershell -ExecutionPolicy Bypass -File C:\MamidLabs-src\scripts\install-mamidlabs.ps1 -Profile lite
   ```
   The script runs these stages in order:

   | Stage | Job |
   |---|---|
   | `preflight` | Windows, GPU, RAM, disk, Python and Git check |
   | `layout` | Folder layout, program files, desktop shortcut |
   | `runtime` | Bundled PowerShell 7.6.6 |
   | `llama` | llama.cpp b10998 (CUDA 13.4) |
   | `python` | App and media Python environments (pinned versions) |
   | `comfyui` | ComfyUI `b0f4b7b`, ComfyUI-GGUF `6ea2651` and comfyui-krea2edit `86f886d` (photo editing) |
   | `verify` | Presence and run check of the components |

   The default install root is `%LOCALAPPDATA%\Programs\MamidLabs`; it can be installed into another SSD folder with
   `-Root <path>`. Use something like `-Stage comfyui` to run a single stage, and `-SkipMedia` to
   skip the media components.
3. Open the app with the **MamidLabs** shortcut on the desktop.
4. Download the models with **Profiles → Profile manager → Download missing**. An interrupted
   download resumes where it left off; an existing file is not downloaded again.
5. For OCR (optional, once):
   ```powershell
   %LOCALAPPDATA%\Programs\MamidLabs\runtime\pwsh\pwsh.exe -File %LOCALAPPDATA%\Programs\MamidLabs\scripts\install-ocr.ps1 -Warmup
   ```

## Updating

**From the app:** Profiles → Profile manager → **Check for updates**. The button compares the latest
release on GitHub with the installed version. If a new version exists, **Update now** downloads the
setup wizard (the size is verified), starts it and closes MamidLabs; the wizard updates the same
folder. Chats, settings and models are kept.

**For a script installation:**

1. Close MamidLabs; make sure no `llama-server.exe` is left in Task Manager.
2. Update the source and run the installation again:
   ```powershell
   git -C C:\MamidLabs-src pull
   %LOCALAPPDATA%\Programs\MamidLabs\runtime\pwsh\pwsh.exe -NoProfile -ExecutionPolicy Bypass -File C:\MamidLabs-src\scripts\install-mamidlabs.ps1 -Profile lite
   ```
   - The `settings\` and `chats\` folders are not touched; the program files are copied again.
   - If ComfyUI and ComfyUI-GGUF are not at the pinned version they are brought to it. Local
     changes are first backed up as a `.patch` under `%LOCALAPPDATA%\Programs\MamidLabs\tools\backup\`.
3. Complete any new model files with **Download missing** in the app.

## Verification

The **end-to-end smoke test** tries the chat, document, OCR, image, photo editing, post-media model
switch and video components with the real models:

```powershell
%LOCALAPPDATA%\Programs\MamidLabs\runtime\pwsh\pwsh.exe -NoProfile -ExecutionPolicy Bypass -File %LOCALAPPDATA%\Programs\MamidLabs\scripts\smoke-test.ps1 -Profile lite
```

Expected result: all steps `[PASS]`; if the Identity Edit LoRA is not installed, photo editing is
`[SKIP]`. Steps can be skipped with `-SkipChat`, `-SkipDocument`, `-SkipOcr`, `-SkipImage`,
`-SkipEdit`, `-SkipVideo`. The result is also written to `logs\smoke-test-last.json`.

**Post-media model switch (8 GB LITE card):** the image step unloads the language model from VRAM
just like the app does and reloads it after generation. Then the chat speed is compared with the
first measurement. If the speed drops below 70% (`-MinSpeedRatio`) the model has spilled into shared
memory and the step fails. The detail line also shows the free VRAM and whether ComfyUI stayed open.

The **unit tests** run in the source folder; no extra environment setup is needed:

```powershell
cd C:\MamidLabs-src
python -m pip install pytest
python -m pytest -q
```

## Uninstalling

**Settings → Apps → MamidLabs → Uninstall** removes MamidLabs without leaving a trace; the computer
stays as if it had never been installed. The same uninstaller (`scripts\uninstall-mamidlabs.ps1`)
runs for installations made with the wizard and with the script:

| Removed | Detail |
|---|---|
| Running processes | The app, llama-server and ComfyUI are stopped first |
| Install folder | Program, bundled PowerShell, llama.cpp, Python environments, ComfyUI, models, pip / Hugging Face / OCR caches, logs, chats, outputs, settings and the encrypted API keys |
| Desktop shortcut | Only if it points to this installation |
| Windows uninstall entry | The row in Settings → Apps |
| Prerequisites | Python, Git, 7-Zip and the VC++ Redistributable; only if the wizard installed them itself with winget (`settings\install-record.json`). Ones installed beforehand are not touched |

Files outside the install folder are not touched: your own models chosen by file path stay where
they are. If the folder contains files that do not belong to MamidLabs you are asked before they are
deleted. The wizard's uninstaller asks whether you want to keep your chats, generated files and
settings.

With the script:

```powershell
# First see what would be deleted (nothing is deleted)
powershell -ExecutionPolicy Bypass -File %LOCALAPPDATA%\Programs\MamidLabs\scripts\uninstall-mamidlabs.ps1 -DryRun
# Uninstall (asks for confirmation)
powershell -ExecutionPolicy Bypass -File %LOCALAPPDATA%\Programs\MamidLabs\scripts\uninstall-mamidlabs.ps1
```

| Option | Effect |
|---|---|
| `-KeepUserData` | `chats`, `outputs`, `settings` are kept |
| `-KeepPrerequisites` | The prerequisites MamidLabs installed are not removed |
| `-PurgeSharedCaches` | Also deletes the shared Python caches outside the install folder: `%LOCALAPPDATA%\pip\cache`, `~\.cache\huggingface\xet`, `%TEMP%\torchinductor_*`. Since other Python projects may use them too, they are not deleted by default; MamidLabs keeps its own pip and Hugging Face caches inside the install folder. |
| `-Yes` | Does not ask for confirmation |

The uninstaller runs with Windows PowerShell 5.1; since the bundled PowerShell 7 is deleted too, it
does not use it.

## Managing the model server manually

```powershell
%LOCALAPPDATA%\Programs\MamidLabs\runtime\pwsh\pwsh.exe -File %LOCALAPPDATA%\Programs\MamidLabs\scripts\manage-qwen.ps1 -Action switch -Profile max
%LOCALAPPDATA%\Programs\MamidLabs\runtime\pwsh\pwsh.exe -File %LOCALAPPDATA%\Programs\MamidLabs\scripts\manage-qwen.ps1 -Action stop
```

`-Action` values: `start`, `switch`, `stop`. Profile values: `max`, `flash`, `lite`.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Not enough free VRAM` error | Another application is using the graphics card. Close applications that use the GPU, such as browser hardware acceleration, or choose a smaller profile. An idle ComfyUI is closed automatically. |
| Chat is slower than expected | `scripts\diagnose-latency.ps1` measures VRAM usage, cache hits and generation speed. On a laptop the speed drops on battery. |
| OCR does not work | Install the OCR environment with `install-ocr.ps1 -Warmup`; the smoke test verifies the OCR step. |
| No image or video can be generated | Check the `logs\comfyui.stderr.log` file. `WinError 10022` lines are harmless. |
| "The photo editing LoRA was not found" | Download the LoRA with **Download missing** in the Profile manager (`krea2_identity_edit_v1_2.safetensors`, [conradlocke/krea2-identity-edit](https://huggingface.co/conradlocke/krea2-identity-edit), ~1.7 GB). |
| "The comfyui-krea2edit node pack for photo editing is not installed" | Install the node pack with `install-mamidlabs.ps1 -Stage comfyui`. |
| The shortcut does not work | Run the installation again; the shortcut opens the app through `launch-qwen-desktop.vbs` with the bundled PowerShell 7. |
