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
    """Measured on an RTX 5060 Ti 16 GB: ~15008 MiB free before loading, the MAX language
    model file 14600 MiB, real use on the GPU ~14640 MiB. The old +2560 MiB margin
    asked for 17160+ MiB and never started MAX on its target hardware."""
    import re

    manager = read_text("scripts/manage-qwen.ps1")
    match = re.search(r"\$overheadMiB = (-?\d+)\s*$", manager, re.MULTILINE)
    assert match, "overheadMiB line not found"
    measured_free_mib = 15008
    max_model_mib = 14600
    assert max_model_mib + int(match.group(1)) <= measured_free_mib


def test_max_projector_runs_on_cpu_and_stays_out_of_vram_budget():
    """mmproj F16 needs 1133 MiB on the GPU; together with the language model it overflowed
    the 16 GB card and fell back to shared memory (chat 19.6 tok/s, image 56 s).
    On the CPU: chat 24.8 tok/s, image 30 s."""
    manager = read_text("scripts/manage-qwen.ps1")
    assert "'--mmproj', $projector, '--no-mmproj-offload'" in manager
    assert "$profileName -eq 'lite' -and $projectorPath" in manager
