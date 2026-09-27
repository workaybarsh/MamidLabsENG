"""Long-lived child processes must not inherit the caller's stdout pipe.

On a real machine: "smoke-test.ps1 *> file", "manage-qwen.ps1 | Out-String" and
Start-Process -Wait never finished while llama-server/ComfyUI was running, because
Start-Process -Redirect* gave the child the caller's inheritable handles.

The criterion does not depend on timing: however slowly the calling process starts (on CI
PowerShell and Add-Type can be slow) it is expected to finish; then it is checked whether
the pipe closed. If the pipe stays open while the child is still running, the child inherited it.
"""
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHILD_SECONDS = 60  # Much longer than the measurement window: the pipe state is measured while the child lives.


def pipe_held_after_caller_exits(tmp_path, launch_line: str):
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh not found")
    script = tmp_path / "launch.ps1"
    script.write_text(
        f"""
. '{ROOT}\\agent\\process-launch.ps1'
$childArgs = @('-NoProfile', '-Command', 'Start-Sleep -Seconds {CHILD_SECONDS}')
{launch_line}
Write-Output "child=$($child.Id)"
""",
        encoding="utf-8-sig",
    )
    proc = subprocess.Popen(
        [pwsh, "-NoProfile", "-NonInteractive", "-File", str(script)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8", errors="replace",
    )
    lines = []
    reader = threading.Thread(target=lambda: lines.extend(proc.stdout), daemon=True)
    reader.start()
    try:
        proc.wait(timeout=90)  # The caller itself may be slow; this does not count as a hang.
        # The caller finished: the pipe stays open only if another process holds it.
        reader.join(timeout=4)
        held = reader.is_alive()
    finally:
        if proc.poll() is None:
            proc.kill()
        child = next((line.strip().split("=", 1)[1] for line in list(lines) if line.startswith("child=")), "")
        if child.isdigit():
            subprocess.run(["taskkill", "/PID", child, "/T", "/F"], capture_output=True, check=False)
        reader.join(timeout=10)
    output = "".join(lines)
    assert "child=" in output, output
    return held, output


def test_detached_child_does_not_hold_callers_pipe(tmp_path):
    held, output = pipe_held_after_caller_exits(
        tmp_path,
        f"$child = Start-DetachedProcess (Get-Process -Id $PID).Path $childArgs $env:TEMP "
        f"'{tmp_path}\\c.out' '{tmp_path}\\c.err'",
    )
    assert not held, output


def test_plain_start_process_redirect_holds_the_pipe(tmp_path):
    """Shows that the old method really produces the problem (the proof of the fix)."""
    held, output = pipe_held_after_caller_exits(
        tmp_path,
        f"$child = Start-Process -FilePath (Get-Process -Id $PID).Path -ArgumentList $childArgs "
        f"-RedirectStandardOutput '{tmp_path}\\c.out' -RedirectStandardError '{tmp_path}\\c.err' "
        f"-WindowStyle Hidden -PassThru",
    )
    assert held, output


def test_long_lived_servers_use_detached_launch():
    manage = (ROOT / "scripts" / "manage-qwen.ps1").read_text(encoding="utf-8-sig")
    swap = (ROOT / "agent" / "media-model-swap.ps1").read_text(encoding="utf-8-sig")
    assert "$child = Start-DetachedProcess $server" in manage
    assert "$comfy = Start-DetachedProcess $python" in swap
    assert "-RedirectStandardOutput" not in manage
    assert "-RedirectStandardOutput" not in swap
