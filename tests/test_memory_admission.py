"""VRAM-based admission (manage-mamidlabs.ps1 Test-MemoryBudget).

Required memory = model file + a safety margin; the mmproj runs on the CPU in all profiles
and does not count toward the VRAM budget. Before measuring,
our own big CUDA context (ComfyUI) is ALWAYS closed if its queue is empty, so the free
figure is trustworthy and the model does not spill into shared memory; if free VRAM is
still not enough a clear error is shown. Otherwise layers spill into Windows shared
memory and chat slows down dramatically.
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8-sig")


def run_pwsh_functions(script: str) -> str:
    """Runs PowerShell functions in a real pwsh (functions extracted from manage-mamidlabs.ps1
    are loaded via the `Get-Functions` prelude)."""
    import shutil
    import subprocess

    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")
    prelude = r"""
$ErrorActionPreference = 'Stop'
function Get-Functions([string]$path) {
    $tokens=$null; $errs=$null
    $ast = [Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errs)
    if ($errs.Count) { throw ($errs | Out-String) }
    return (($ast.EndBlock.Statements | Where-Object { $_ -is [Management.Automation.Language.FunctionDefinitionAst] } | ForEach-Object { $_.Extent.Text }) -join "`n")
}
"""
    proc = subprocess.run(
        [pwsh, "-NoProfile", "-Command", prelude + script],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    if proc.returncode != 0:
        raise AssertionError(f"pwsh failed: {proc.stderr}")
    return proc.stdout


def test_memory_budget_function_exists_and_is_called_before_start():
    manager = read_text("scripts/manage-mamidlabs.ps1")
    assert "function Test-MemoryBudget" in manager
    # The projector is NOT part of the budget: it runs on the CPU in all three profiles.
    assert "Test-MemoryBudget $model $Profile $projector $candidate" in manager
    # Single GPU owner: our CUDA context (ComfyUI) is handled before measuring. The default
    # keeps it open; duplicates/leaks are closed; if the budget fails it is closed and retried.
    # See test_idle_comfy_is_kept_open_but_leaks_are_cleaned.
    assert "[void](Stop-IdleComfy)" in manager
    # The check must run AFTER the file existence checks (when the model path is known) but
    # BEFORE the model process is started; closing ComfyUI must come before the measurement.
    idx_comfy = manager.index("[void](Stop-IdleComfy)")
    idx_check = manager.index("Test-MemoryBudget $model $Profile $projector $candidate")
    idx_start = manager.index("$child = Start-DetachedProcess $server")
    assert idx_comfy < idx_check < idx_start


def test_idle_comfy_policy_is_profile_aware():
    """ComfyUI lifecycle. DEFAULT OFF: ComfyUI runs only during media generation and is closed
    when the job ends and before the language model loads — it holds no VRAM/RAM while idle.
    Rationale: the LITE target machine is 8 GB VRAM / 16 GB RAM; the 9B is at the card's limit
    and the ComfyUI context spills it. On a 16 GB card LITE users can keep it open with
    `comfyKeepAlive=true`; MAX/FLASH always close. In API mode the script is not called."""
    manager = read_text("scripts/manage-mamidlabs.ps1")
    # Default OFF (no idle VRAM/RAM).
    assert "$comfyKeepAlive = $false" in manager
    assert "$settings.comfyKeepAlive" in manager
    assert "function Get-ComfyInstanceRoots" in manager
    assert "function Stop-ComfyLeaks" in manager
    assert "[void](Stop-ComfyLeaks)" in manager
    # Default off -> MAX/FLASH and LITE close; only opt-in (true) + LITE stays open.
    assert "if (!$comfyKeepAlive -or $Profile -ne 'lite') { [void](Stop-IdleComfy) }" in manager
    # If the budget fails, close and retry (safety net preserved); WDDM may release VRAM late
    # after ASR/OCR, so the check is retried a bounded number of times (fails on the last one).
    assert "if ($attempt -eq 1 -and (Stop-IdleComfy)) { continue }" in manager
    assert "if (!$budgetOk) { throw $lastBudgetError }" in manager


def test_idle_comfy_is_only_stopped_when_its_queue_is_empty():
    manager = read_text("scripts/manage-mamidlabs.ps1")
    stop = manager.split("function Stop-IdleComfy")[1].split("function Get-ManagedServers")[0]
    assert "http://127.0.0.1:8188/queue" in stop
    assert "@($queue.queue_running).Count -gt 0 -or @($queue.queue_pending).Count -gt 0) { return $false }" in stop


def test_memory_budget_uses_measured_free_vram_and_estimated_need():
    manager = read_text("scripts/manage-mamidlabs.ps1")
    assert "memory.free" in manager
    assert "(Get-Item -LiteralPath $modelPath).Length" in manager
    assert "$freeMiB -lt $neededMiB" in manager


def test_memory_budget_skips_cleanly_when_no_nvidia_gpu():
    """Without nvidia-smi (AMD/Intel GPU or no driver installed) the check must be
    skipped silently - it must not block the installation, llama-server will give
    the real error anyway."""
    manager = read_text("scripts/manage-mamidlabs.ps1")
    assert "if (!$smi) { return }" in manager


def test_start_action_accepts_skip_memory_check_switch():
    manager = read_text("scripts/manage-mamidlabs.ps1")
    assert "[switch]$SkipMemoryCheck" in manager


def test_max_budget_margin_is_context_aware_and_prevents_spill():
    """Measured on an RTX 5060 Ti 16 GB with the 27B, from llama.cpp's OWN report (b10998, q4_0
    KV, MTP on): CUDA0 model buffer 13945 MiB, KV cache 576 MiB @32768 / 288 @16384,
    recurrent-state buffer 598.5 MiB (fixed), compute buffer ~112 MiB. The non-weight overhead is
    therefore ~1200 MiB at 32K but only ~800 MiB at 16K. The old fixed 750 MiB margin
    (threshold 15350) admitted 32K with only ~15.50 GiB free: it SPILLED ~400 MiB into Windows
    shared memory and chat dropped from ~40 to ~27 tok/s. Measured footprints (dedicated+shared)
    are 15324 MiB @16384 (no spill, ~40 tok/s) and ~15770 @32768 (spilled)."""
    import re

    manager = read_text("scripts/manage-mamidlabs.ps1")
    assert "function Get-ModelOverheadMiB" in manager
    m32 = re.search(r"if \(\$ctx -ge 32768\) \{ return (\d+) \}", manager)
    m16 = re.search(r"if \(\$ctx -ge 16384\) \{ return (\d+) \}", manager)
    assert m32, "32K overhead entry not found"
    assert m16, "16K overhead entry not found"
    model_mib = 14600
    threshold_32k = model_mib + int(m32.group(1))  # 15800
    threshold_16k = model_mib + int(m16.group(1))  # 15400
    free_normal_desktop = 15497  # measured with only the Windows desktop running
    assert threshold_16k <= free_normal_desktop, "16K must fit a normal desktop"
    assert threshold_32k > free_normal_desktop, "32K must NOT load when it would spill"
    assert threshold_32k >= 15770, "the 32K margin must cover the measured spilled footprint"


def test_max_flash_falls_back_from_32k_to_16k_context():
    """MAX/FLASH tries 32K first and falls back to 16K when 32K does not fully fit VRAM (it used
    to spill into shared memory). The chosen context feeds both the memory gate and --ctx-size."""
    manager = read_text("scripts/manage-mamidlabs.ps1")
    assert "$ctxCandidates = if ($Profile -eq 'lite') { @([int]$liteCtx) } else { @(32768, 16384) }" in manager
    assert "foreach ($candidate in $ctxCandidates)" in manager
    assert "if ($chosenCtx) { $modelCtx = $chosenCtx; $budgetOk = $true; break }" in manager
    assert "'--ctx-size', \"$modelCtx\", '--predict', $predict, '--parallel', '1'" in manager


def test_lite_budget_is_not_tightened_by_the_max_fix():
    """The ctx-aware margin (and the 32K -> 16K fallback) applies only to the 16 GB profiles.
    LITE runs at the limit on an 8 GB card and llama.cpp absorbs a small overflow; enlarging
    the margin there would reject a working LITE install. So the LITE margin stays 300 MiB
    and its tolerance stays 1024."""
    manager = read_text("scripts/manage-mamidlabs.ps1")
    assert "if ($profileName -eq 'lite') { return 300 }" in manager
    tol = next((l for l in manager.splitlines() if "$toleranceMiB = if" in l), "")
    assert "{ 1024 } else { 0 }" in tol


def test_projector_runs_on_cpu_and_stays_out_of_vram_budget():
    """MAX/FLASH: mmproj F16 needs 1133 MiB on the GPU; together with the language model it
    overflowed the 16 GB card and fell back to shared memory (chat 19.6 tok/s, image 56 s).
    On the CPU: chat 24.8 tok/s, image 30 s. LITE: the Q8_0 mmproj of 595 MiB did not fit an
    8 GB card on the GPU and was pushing the big embedding tensor onto the CPU; on the CPU
    VRAM drops to 7249/8188 MiB (708 MiB free) and chat speed is unchanged. So the mmproj
    runs on the CPU in all three profiles and does not count toward the VRAM budget."""
    manager = read_text("scripts/manage-mamidlabs.ps1")
    # --no-mmproj-offload is used in both profile branches (LITE + MAX/FLASH).
    assert manager.count("'--mmproj', $projector, '--no-mmproj-offload'") >= 2
    # The Lite budget no longer ADDS the projector size to the model size (on the CPU, not in VRAM).
    assert "$profileName -eq 'lite' -and $projectorPath" not in manager
    assert "$modelMiB += [math]::Ceiling((Get-Item -LiteralPath $projectorPath).Length / 1MB)" not in manager


def test_memory_budget_tolerates_small_overflow_for_mtp_grown_lite():
    """The MTP head grows the LITE model by ~464 MiB; on an 8 GB card the check used to say
    "does not fit" and never start the model. In practice llama.cpp keeps the big embedding
    tensor on the CPU and tolerates a small overflow (measured: loaded at ~165 MiB over,
    ~41 tok/s). A small overflow must not block; only a clear overflow (~1 GB over the margin)
    stops it."""
    manager = read_text("scripts/manage-mamidlabs.ps1")
    assert "$toleranceMiB = if ($profileName -eq 'lite') { 1024 } else { 0 }" in manager
    assert "if ($freeMiB -lt ($neededMiB - $toleranceMiB))" in manager
    assert "Write-Warning" in manager


def test_orphan_app_gpu_workers_are_cleaned_before_model_load():
    """BEFORE the model loads, leftover app-owned GPU workers (an idle persistent audio
    worker `whisper-tools.py --serve` and the OCR worker `ocr-tools.py`) are closed
    automatically. After a crash or forced close the remaining CUDA contexts (~140-600 MiB)
    can eat the language model's thin margin and cause "not enough VRAM". The cleanup only
    targets processes under the install root; other applications are never touched."""
    manager = read_text("scripts/manage-mamidlabs.ps1")
    assert "function Get-AppGpuWorkers" in manager
    assert "function Stop-OrphanWorkers" in manager
    # GPU worker scripts are targeted (audio + OCR); ComfyUI has its own lifecycle (main.py).
    assert "(whisper-tools|ocr-tools)" in manager
    # Runs BEFORE the budget check (VRAM cleanup before the model process starts).
    idx_clean = manager.index("[void](Stop-OrphanWorkers)")
    idx_check = manager.index("Test-MemoryBudget $model $Profile $projector $candidate")
    assert idx_clean < idx_check


def test_orphan_worker_selection_only_matches_app_gpu_workers():
    """Behavior (real pwsh): Get-AppGpuWorkers selects ONLY audio/OCR workers whose python
    lives under the install root; it excludes python from another root, ComfyUI, and tools
    that do not use the GPU (mamidlabs-agent-tools.py)."""
    script = r"""
$root = 'C:\fake\root'
function Get-CimInstance { param([string]$Class, [string]$Filter, $ErrorAction) $script:fake }
. ([scriptblock]::Create((Get-Functions 'MANAGER_PATH')))
$script:fake = @(
  [pscustomobject]@{ ProcessId=111; ExecutablePath='C:\fake\root\venv\asr\Scripts\python.exe'; CommandLine='"C:\fake\root\venv\asr\Scripts\python.exe" "C:\fake\root\agent\whisper-tools.py" --serve' },
  [pscustomobject]@{ ProcessId=222; ExecutablePath='C:\fake\root\venv\ocr\Scripts\python.exe'; CommandLine='"C:\fake\root\venv\ocr\Scripts\python.exe" "C:\fake\root\agent\ocr-tools.py"' },
  [pscustomobject]@{ ProcessId=333; ExecutablePath='C:\Other\python.exe'; CommandLine='whisper-tools.py' },
  [pscustomobject]@{ ProcessId=444; ExecutablePath='C:\fake\root\venv\app\Scripts\python.exe'; CommandLine='"C:\fake\root\agent\mamidlabs-agent-tools.py"' },
  [pscustomobject]@{ ProcessId=555; ExecutablePath='C:\fake\root\venv\imagegen\Scripts\python.exe'; CommandLine='C:\fake\root\ComfyUI\main.py' }
)
(Get-AppGpuWorkers | ForEach-Object { [int]$_.ProcessId } | Sort-Object) -join ','
"""
    script = script.replace("MANAGER_PATH", str(ROOT / "scripts" / "manage-mamidlabs.ps1"))
    out = run_pwsh_functions(script)
    assert out.strip() == "111,222", out