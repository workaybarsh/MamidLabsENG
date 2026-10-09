"""VRAM-based admission (manage-qwen.ps1 Test-MemoryBudget).

Required memory = model file (+ projector on LITE) + a safety margin. If free VRAM is
not enough, an idle ComfyUI is closed and memory is measured again; if it is still not
enough, llama-server is not started and a clear error is shown. Otherwise layers spill
into Windows shared memory and chat slows down dramatically.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8-sig")


def test_memory_budget_function_exists_and_is_called_before_start():
    manager = read_text("scripts/manage-qwen.ps1")
    assert "function Test-MemoryBudget" in manager
    # The image projector is included in the budget since it is also loaded into VRAM.
    assert "try { Test-MemoryBudget $model $Profile $projector }" in manager
    # If there is no room, an idle ComfyUI is closed first, then it is measured once more;
    # if it is still not enough the first error is passed on as is.
    assert "if (!(Stop-IdleComfy)) { throw }" in manager
    # The check must run AFTER the file existence checks (when the model path is known) but
    # BEFORE the model process is started.
    idx_check = manager.index("Test-MemoryBudget $model $Profile $projector")
    idx_start = manager.index("$child = Start-DetachedProcess $server")
    assert idx_check < idx_start


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


def test_max_budget_fits_a_16_gb_card():
    """Measured on an RTX 5060 Ti 16 GB: the model needs ~15422-15442 MiB on the GPU; an idle
    ComfyUI pulls free VRAM down to ~15020 MiB (the model no longer fits, ~6 tok/s). The
    MAX/FLASH budget must be model + 800 MiB (needed 15400 MiB): it fits with ComfyUI closed
    (~15442-15670 free) but not with ComfyUI open (~15020 free), so ComfyUI is closed."""
    import re

    manager = read_text("scripts/manage-qwen.ps1")
    match = re.search(r"\$overheadMiB = if \(\$profileName -eq 'lite'\) \{ (\d+) \} else \{ (\d+) \}", manager)
    assert match, "profile-aware overheadMiB line not found"
    overhead = int(match.group(2))  # else branch = MAX/FLASH
    assert overhead == 800
    max_model_mib = 14600
    measured_free_with_comfy_open = 15020
    measured_free_with_comfy_closed = 15442
    needed = max_model_mib + overhead  # 15400
    assert needed <= measured_free_with_comfy_closed
    assert needed > measured_free_with_comfy_open
    # A 1000 MiB margin (threshold 15600) even rejected the machine's normal free value.
    assert needed < max_model_mib + 1000


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


def test_max_projector_runs_on_cpu_and_stays_out_of_vram_budget():
    """mmproj F16 needs 1133 MiB on the GPU; together with the language model it overflowed
    the 16 GB card and fell back to shared memory (chat 19.6 tok/s, image 56 s).
    On the CPU: chat 24.8 tok/s, image 30 s."""
    manager = read_text("scripts/manage-qwen.ps1")
    assert "'--mmproj', $projector, '--no-mmproj-offload'" in manager
    assert "$profileName -eq 'lite' -and $projectorPath" in manager


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