"""7-Zip (RAR extraction) and Visual C++ Redistributable prerequisites.

Finding: even with the `rarfile` package installed, the real RAR-extraction tool
(`7z`/`unrar`) was never installed anywhere - office-tools.py only looked at a
`tools\\7zip\\7z.exe` path that never existed. Likewise the Visual C++ Redistributable
that native builds (e.g. `llama-server.exe`) usually need was never checked at all.
Fix: both are now installed automatically with winget, like the other prerequisites
(Python, Git); office-tools.py looks in the standard Program Files locations.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8-sig")


def run_pwsh_functions(script: str) -> str:
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
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    if proc.returncode != 0:
        raise AssertionError(f"pwsh failed: {proc.stderr}")
    return proc.stdout


def test_setup_run_installs_7zip_and_vcredist_via_winget():
    setup_run = read_text("scripts/setup-run.ps1")
    assert "Install-WithWinget '7zip.7zip' '7-Zip'" in setup_run
    assert "Install-WithWinget 'Microsoft.VCRedist.2015+.x64' 'Visual C++ Redistributable'" in setup_run
    # A missing one must not stop setup entirely (missing RAR extraction/VC++
    # Redistributable does not block chat or image generation) - only a note is left.
    assert "7-Zip missing; RAR extraction may not work" in setup_run


def test_office_tools_checks_standard_program_files_locations_for_sevenzip():
    office_tools = read_text("agent/office-tools.py")
    assert "ProgramFiles" in office_tools
    assert "ProgramFiles(x86)" in office_tools
    assert "7-Zip" in office_tools


def test_install_ocr_falls_back_to_official_paddle_channel_and_checks_exit_codes():
    """paddle-qa.bj.bcebos.com is a QA/nightly server with no permanence guarantee like
    PyPI; if it is ever removed, fall back to the official stable channel
    (paddlepaddle.org.cn). Also, pip calls' $LASTEXITCODE must be checked - it used not
    to be, so a failed install could silently count as "successful"."""
    install_ocr = read_text("scripts/install-ocr.ps1")
    assert "paddlepaddle.org.cn/packages/stable" in install_ocr
    assert install_ocr.count("if ($LASTEXITCODE -ne 0)") >= 3


def test_vcredist_check_is_false_and_does_not_throw_when_windir_is_missing():
    """On Linux/restricted environments WINDIR may be undefined; passing null directly
    to Join-Path throws a terminating error (found and fixed on real CI).
    Test-VCRedistReady looks at WINDIR, so without it the result is deterministically
    False."""
    out = run_pwsh_functions(rf"""
Invoke-Expression (Get-Functions '{ROOT / "scripts" / "setup-run.ps1"}')
Remove-Item Env:\WINDIR -ErrorAction SilentlyContinue
Write-Output ('vc:' + (Test-VCRedistReady))
""")
    assert "vc:False" in out


def test_seven_zip_check_does_not_throw_when_program_files_env_vars_are_missing():
    """Test-SevenZipReady looks at ProgramFiles/ProgramFiles(x86); passing null to
    Join-Path when they're undefined threw a terminating error (found and fixed on
    real CI). The result itself (True/False) depends on the machine - e.g. GitHub's
    windows-latest runner already ships with 7-Zip installed - so only "does not
    crash" is checked here, not a specific value."""
    out = run_pwsh_functions(rf"""
Invoke-Expression (Get-Functions '{ROOT / "scripts" / "setup-run.ps1"}')
Remove-Item Env:\ProgramFiles -ErrorAction SilentlyContinue
Remove-Item 'Env:\ProgramFiles(x86)' -ErrorAction SilentlyContinue
Write-Output ('7zip:' + (Test-SevenZipReady))
""")
    assert re.search(r"7zip:(True|False)", out)
