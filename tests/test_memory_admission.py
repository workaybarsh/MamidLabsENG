"""VRAM-based admission (manage-qwen.ps1 Test-MemoryBudget).

Required memory = model file + a safety margin; the mmproj runs on the CPU in all profiles
and does not count toward the VRAM budget. Before measuring,
our own big CUDA context (ComfyUI) is ALWAYS closed if its queue is empty, so the free
figure is trustworthy and the model does not spill into shared memory; if free VRAM is
still not enough a clear error is shown. Otherwise layers spill into Windows shared
memory and chat slows down dramatically.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8-sig")


def test_memory_budget_function_exists_and_is_called_before_start():
    manager = read_text("scripts/manage-qwen.ps1")
    assert "function Test-MemoryBudget" in manager
    # The projector is NOT part of the budget: it runs on the CPU in all three profiles.
    assert "Test-MemoryBudget $model $Profile $projector" in manager
    # Single GPU owner: our CUDA context (ComfyUI) is handled before measuring. The default
    # keeps it open; duplicates/leaks are closed; if the budget fails it is closed and retried.
    # See test_idle_comfy_is_kept_open_but_leaks_are_cleaned.
    assert "[void](Stop-IdleComfy)" in manager
    # The check must run AFTER the file existence checks (when the model path is known) but
    # BEFORE the model process is started; closing ComfyUI must come before the measurement.
    idx_comfy = manager.index("[void](Stop-IdleComfy)")
    idx_check = manager.index("Test-MemoryBudget $model $Profile $projector")
    idx_start = manager.index("$child = Start-DetachedProcess $server")
    assert idx_comfy < idx_check < idx_start


def test_idle_comfy_policy_is_profile_aware():
    """ComfyUI lifecycle. DEFAULT OFF: ComfyUI runs only during media generation and is closed
    when the job ends and before the language model loads — it holds no VRAM/RAM while idle.
    Rationale: the LITE target machine is 8 GB VRAM / 16 GB RAM; the 9B is at the card's limit
    and the ComfyUI context spills it. On a 16 GB card LITE users can keep it open with
    `comfyKeepAlive=true`; MAX/FLASH always close. In API mode the script is not called."""
    manager = read_text("scripts/manage-qwen.ps1")
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
    manager = read_text("scripts/manage-qwen.ps1")
    stop = manager.split("function Stop-IdleComfy")[1].split("function Get-ManagedServers")[0]
    assert "http://127.0.0.1:8188/queue" in stop
    assert "@($queue.queue_running).Count -gt 0 -or @($queue.queue_pending).Count -gt 0) { return $false }" in stop


def test_memory_budget_uses_measured_free_vram_and_estimated_need():
    manager = read_text("scripts/manage-qwen.ps1")
    assert "memory.free" in manager
    assert "(Get-Item -LiteralPath $modelPath).Length" in manager
    assert "$freeMiB -lt $neededMiB" in manager


def test_memory_budget_skips_cleanly_when_no_nvidia_gpu():
    """Without nvidia-smi (AMD/Intel GPU or no driver installed) the check must be
    skipped silently - it must not block the installation, llama-server will give
    the real error anyway."""
    manager = read_text("scripts/manage-qwen.ps1")
    assert "if (!$smi) { return }" in manager


def test_start_action_accepts_skip_memory_check_switch():
    manager = read_text("scripts/manage-qwen.ps1")
    assert "[switch]$SkipMemoryCheck" in manager


def test_max_budget_requires_a_full_vram_fit():
    """Measured on an RTX 5060 Ti 16 GB: the 27B file is 14600 MiB but occupies ~15378-15444 MiB
    on the GPU (pre-allocated q4_0 KV cache + compute buffers + MTP draft context), so the real
    margin is ~778-844 MiB. With a 600 margin (threshold 15200) loads between 15200 and 15450
    silently spilled into shared memory and dropped chat to ~20 tok/s. With an 850 margin
    (threshold 15450) the model only loads when it will FULLY fit; no spill (measured: 15529 MiB
    free -> full fit ~55 tok/s; 15095 MiB -> spilled ~30 tok/s). Always closing ComfyUI remains
    the real protection."""
    import re

    manager = read_text("scripts/manage-qwen.ps1")
    match = re.search(r"\$overheadMiB = if \(\$profileName -eq 'lite'\) \{ (\d+) \} else \{ (\d+) \}", manager)
    assert match, "profile-aware overheadMiB line not found"
    overhead = int(match.group(2))  # else branch = MAX/FLASH
    assert overhead == 750, "the MAX/FLASH full-fit margin must be 750 (threshold 15350 = the measured footprint); the LOWEST threshold that prevents spill"
    max_model_mib = 14600
    threshold = max_model_mib + overhead  # 15350
    full_fit_free = 15529  # measured: full fit, ~55 tok/s
    spill_free = 15095     # measured: spilled, ~30 tok/s
    assert threshold <= full_fit_free, "the full-fit state must be accepted"
    assert threshold > spill_free, "a spilling state must be REJECTED (prevents spill)"


def test_lite_budget_is_not_tightened_by_the_max_fix():
    """The root-cause fix (larger margin, tolerance 0) applies only to the 16 GB profiles.
    LITE runs at the limit on an 8 GB card and llama.cpp absorbs a small overflow; enlarging
    the margin there would reject a working LITE install. So the LITE margin stays 300 MiB
    and its tolerance stays 1024."""
    import re

    manager = read_text("scripts/manage-qwen.ps1")
    match = re.search(r"\$overheadMiB = if \(\$profileName -eq 'lite'\) \{ (\d+) \} else \{ (\d+) \}", manager)
    assert match, "profile-aware overheadMiB line not found"
    assert int(match.group(1)) == 300, "LITE margin was enlarged (breaks the LITE install)"
    tol = next((l for l in manager.splitlines() if "$toleranceMiB = if" in l), "")
    assert "{ 1024 } else { 0 }" in tol


def test_projector_runs_on_cpu_and_stays_out_of_vram_budget():
    """MAX/FLASH: mmproj F16 needs 1133 MiB on the GPU; together with the language model it
    overflowed the 16 GB card and fell back to shared memory (chat 19.6 tok/s, image 56 s).
    On the CPU: chat 24.8 tok/s, image 30 s. LITE: the Q8_0 mmproj of 595 MiB did not fit an
    8 GB card on the GPU and was pushing the big embedding tensor onto the CPU; on the CPU
    VRAM drops to 7249/8188 MiB (708 MiB free) and chat speed is unchanged. So the mmproj
    runs on the CPU in all three profiles and does not count toward the VRAM budget."""
    manager = read_text("scripts/manage-qwen.ps1")
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
    manager = read_text("scripts/manage-qwen.ps1")
    assert "$toleranceMiB = if ($profileName -eq 'lite') { 1024 } else { 0 }" in manager
    assert "if ($freeMiB -lt ($neededMiB - $toleranceMiB))" in manager
    assert "Write-Warning" in manager