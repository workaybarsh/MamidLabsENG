"""Versioned backups for the file-writing tools.

Before write_bound_file or create_document overwrites an existing file, the previous
version is copied to outputs\\backups as <name>.<timestamp>.bak. The scope is
deliberately narrow: run_command's effects are not treated as reversible. The first
write creates no backup; on overwrite the old content must be in the .bak file and the
new content in the target.
"""
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8-sig")


def test_backup_function_exists_and_is_wired_into_both_write_tools():
    inference = read_text("agent/mamidlabs-inference.ps1")
    assert "function Backup-FileBeforeOverwrite" in inference
    assert "$backup = Backup-FileBeforeOverwrite $target" in inference
    assert inference.count("$backup = Backup-FileBeforeOverwrite $target") == 2, (
        "both write_bound_file and create_document must back up"
    )


def test_backup_only_runs_when_a_file_already_exists():
    """On the first write (no file) no backup is needed - the Test-Path check already
    ensures that, but without an explicit test a regression would go unnoticed."""
    inference = read_text("agent/mamidlabs-inference.ps1")
    fn = inference.split("function Backup-FileBeforeOverwrite")[1].split("function Wait-LocalModelStopped")[0]
    assert "if (!(Test-Path -LiteralPath $target -PathType Leaf)) { return $null }" in fn


def test_backup_failure_is_not_silently_swallowed():
    """If the backup fails (disk full, permission problem) it throws ON PURPOSE;
    silently allowing an irreversible write would be wrong."""
    inference = read_text("agent/mamidlabs-inference.ps1")
    fn = inference.split("function Backup-FileBeforeOverwrite")[1].split("function Wait-LocalModelStopped")[0]
    assert "try" not in fn or "catch" not in fn, (
        "Backup-FileBeforeOverwrite must not swallow the error itself; the general "
        "try/catch on the caller side (and the #16/#29 error visibility) must catch it"
    )


def run_pwsh_functions(script: str) -> str:
    pwsh = "pwsh"
    prelude = r"""
$ErrorActionPreference = 'Stop'
function Get-Functions([string]$path) {
    $tokens=$null; $errs=$null
    $ast = [Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errs)
    if ($errs.Count) { throw ($errs | Out-String) }
    return (($ast.EndBlock.Statements | Where-Object { $_ -is [Management.Automation.Language.FunctionDefinitionAst] } | ForEach-Object { $_.Extent.Text }) -join "`n")
}
"""
    full = prelude + script
    try:
        proc = subprocess.run(
            [pwsh, "-NoProfile", "-Command", full],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
    except FileNotFoundError:
        pytest.skip("PowerShell 7 is unavailable")
    if proc.returncode != 0:
        raise AssertionError(f"pwsh failed: {proc.stderr}")
    return proc.stdout


def test_overwrite_backs_up_the_previous_version_with_real_content(tmp_path):
    inference_path = str(ROOT / "agent" / "mamidlabs-inference.ps1").replace("\\", "\\\\")
    folder = str(tmp_path).replace("\\", "\\\\")
    out = run_pwsh_functions(rf"""
$JobData = @{{ root = '{tmp_path}' }}
$Shared = [hashtable]::Synchronized(@{{ AccessMode = 'full'; Cancel = $false }})
Invoke-Expression (Get-Functions '{inference_path}')

$folder = '{folder}'
$script:createdArtifact = $null
$a1 = [pscustomobject]@{{ relative_path = 'rollback-test.txt'; content = 'version 1' }}
$r1 = Invoke-Tool 'write_bound_file' $a1 $folder
Write-Output "r1:$r1"

$a2 = [pscustomobject]@{{ relative_path = 'rollback-test.txt'; content = 'version 2 - changed' }}
$r2 = Invoke-Tool 'write_bound_file' $a2 $folder
Write-Output "r2:$r2"
""")
    lines = {ln.split(":", 1)[0]: ln.split(":", 1)[1] for ln in out.splitlines() if ln.startswith(("r1:", "r2:"))}
    assert "previous version backed up" not in lines["r1"], "the file did not exist on the first write, there must be no backup"
    assert "previous version backed up" in lines["r2"]

    backup_dir = tmp_path / "outputs" / "backups"
    backups = list(backup_dir.glob("rollback-test.txt.*.bak"))
    assert len(backups) == 1, f"exactly one backup file was expected: {backups}"
    assert backups[0].read_text(encoding="utf-8") == "version 1"
    assert (tmp_path / "rollback-test.txt").read_text(encoding="utf-8") == "version 2 - changed"
