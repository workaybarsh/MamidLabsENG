"""Cancelling the installation with rollback.

The wizard starts setup-run.ps1 with ewNoWait and pumps its own message queue by hand
(mamidlabs.iss PumpMessages), so the window stays responsive during installation. If the
cancel is confirmed, taskkill /T ends the child processes, uninstall-mamidlabs.ps1 removes
everything installed so far and the wizard closes. Details: AGENTS.md, pitfalls.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ISS = ROOT / "packaging" / "windows" / "mamidlabs.iss"


def read_text(path) -> str:
    return Path(path).read_text(encoding="utf-8-sig")


def pwsh_or_skip():
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")
    return pwsh


# ------------------------------------------------------------------ cancel + rollback

def test_installer_does_not_block_the_ui_thread_while_running_setup():
    iss = read_text(ISS)
    block = iss[iss.index("procedure CurStepChanged"):]
    assert "ewNoWait" in block
    # A fully blocking Exec stops the message loop and locks the Cancel button.
    assert "ewWaitUntilTerminated, SetupExitCode" not in block


def test_installer_pumps_messages_so_the_cancel_button_stays_responsive():
    iss = read_text(ISS)
    assert "PeekMessageW" in iss and "TranslateMessage" in iss and "DispatchMessageW" in iss
    assert "procedure PumpMessages" in iss
    assert "PumpMessages;" in iss[iss.index("procedure CurStepChanged"):]


def test_installer_confirms_cancel_and_rolls_back_via_the_uninstaller():
    iss = read_text(ISS)
    assert "procedure CancelButtonClick" in iss
    cancel_block = iss[iss.index("procedure CancelButtonClick"):iss.index("procedure CurStepChanged")]
    assert "InstallRunning" in cancel_block
    assert "MsgBox(" in cancel_block and "MB_YESNO" in cancel_block

    step_block = iss[iss.index("procedure CurStepChanged"):]
    # Also kills the child processes (winget, pip, the model download).
    assert "taskkill.exe" in step_block and "/T /F" in step_block
    # Uses the same rollback path (Yes + FromWizard) as CurUninstallStepChanged.
    assert "uninstall-mamidlabs.ps1" in step_block
    assert "-Yes -FromWizard" in step_block or "' -Yes -FromWizard'" in step_block.replace('"', "'")
    # Abort alone is not enough: Inno's step machine moves on to the Summary/Finished pages
    # after ssPostInstall. On cancel the process is terminated directly with ExitProcess.
    assert "ExitProcess(1)" in step_block


def test_setup_run_writes_a_pid_file_and_a_completion_flag_for_the_wizard_to_poll():
    setup_run = read_text(ROOT / "scripts/setup-run.ps1")
    assert "setup.pid" in setup_run
    assert "setup-done.flag" in setup_run
    # Success/failure/unexpected exception - the flag must be written in every case,
    # otherwise the wizard waits forever.
    assert setup_run.count("$doneFlag") >= 3
    assert "trap {" in setup_run


def test_setup_run_pid_and_done_flag_actually_get_written(tmp_path):
    """Actually runs the PID/flag-writing part at the top of the script and verifies it
    (not the whole script - that needs Windows and real downloads)."""
    pwsh = pwsh_or_skip()
    setup_run = read_text(ROOT / "scripts/setup-run.ps1")
    start = setup_run.index("$pidFile = Join-Path")
    end = setup_run.index("$steps = [Collections.Generic.List[object]]::new()")
    snippet = setup_run[start:end]
    logs = tmp_path / "logs"
    logs.mkdir()
    runner = tmp_path / "run.ps1"
    runner.write_text(
        f"$logs = '{logs}'\n" + snippet + "\nWrite-Output ('pid:' + (Get-Content -LiteralPath $pidFile))\n",
        encoding="utf-8-sig",
    )
    result = subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-File", str(runner)],
                            capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "pid:" in result.stdout
    assert not (logs / "setup-done.flag").exists()  # deleted at the start, the script has not finished yet
