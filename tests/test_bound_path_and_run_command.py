"""Bound-folder paths and run_command escaping.

1) Small models sometimes write a full (rooted) path, including the bound folder, into
   relative_path. Join-Path would prepend the folder and produce a doubled, invalid
   path. Resolve-BoundTarget uses a rooted path as-is; a path outside the folder is
   rejected.
2) Commands with nested quotes (powershell -Command "if (...) { ... }") break when
   passed as a /c argument, because .NET's \\" escaping does not match cmd.exe. The
   command is written to a temporary .cmd file and run from there.
3) An unmatched "%" in a .cmd file is silently removed ("42 % 2" -> "42  2"). Matched
   "%VAR%" pairs are kept; a lone "%" is escaped as "%%".
"""
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

PRELUDE = r"""
$ErrorActionPreference = 'Stop'
# Python decodes the output as UTF-8; if the console code page differs (e.g. cp1254
# on Turkish Windows) non-ASCII characters get corrupted and the test becomes
# environment-dependent. Pin the output encoding to make the test deterministic.
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
function Get-Functions([string]$path) {
    $tokens=$null; $errs=$null
    $ast = [Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errs)
    if ($errs.Count) { throw ($errs | Out-String) }
    return (($ast.EndBlock.Statements | Where-Object { $_ -is [Management.Automation.Language.FunctionDefinitionAst] } | ForEach-Object { $_.Extent.Text }) -join "`n")
}
"""


def run_pwsh_functions(script: str) -> str:
    pwsh = "pwsh"
    try:
        proc = subprocess.run(
            [pwsh, "-NoProfile", "-Command", PRELUDE + script],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
    except FileNotFoundError:
        pytest.skip("PowerShell 7 is unavailable")
    if proc.returncode != 0:
        raise AssertionError(f"pwsh failed: {proc.stderr}")
    return proc.stdout


def test_rooted_relative_path_inside_bound_folder_resolves_without_doubling(tmp_path):
    inference_path = str(ROOT / "agent" / "mamidlabs-inference.ps1").replace("\\", "\\\\")
    folder = str(tmp_path).replace("\\", "\\\\")
    rooted = str(tmp_path / "fibonacci.py").replace("\\", "\\\\")
    out = run_pwsh_functions(rf"""
$JobData = @{{ root = '{tmp_path}' }}
$Shared = [hashtable]::Synchronized(@{{ AccessMode = 'full'; Cancel = $false }})
Invoke-Expression (Get-Functions '{inference_path}')
$folder = '{folder}'
$a = [pscustomobject]@{{ relative_path = '{rooted}'; content = 'print(1)' }}
Write-Output ('r:' + (Invoke-Tool 'write_bound_file' $a $folder))
""")
    line = next(ln for ln in out.splitlines() if ln.startswith("r:"))
    assert line.startswith("r:Written"), line
    assert (tmp_path / "fibonacci.py").read_text(encoding="utf-8") == "print(1)"


def test_rooted_relative_path_outside_bound_folder_is_still_rejected(tmp_path):
    """The safety boundary must hold: a rooted path outside the bound folder is still rejected."""
    inference_path = str(ROOT / "agent" / "mamidlabs-inference.ps1").replace("\\", "\\\\")
    bound = tmp_path / "bound"
    bound.mkdir()
    outside = tmp_path / "outside.py"
    folder = str(bound).replace("\\", "\\\\")
    outside_arg = str(outside).replace("\\", "\\\\")
    out = run_pwsh_functions(rf"""
$JobData = @{{ root = '{tmp_path}' }}
$Shared = [hashtable]::Synchronized(@{{ AccessMode = 'full'; Cancel = $false }})
Invoke-Expression (Get-Functions '{inference_path}')
$folder = '{folder}'
$a = [pscustomobject]@{{ relative_path = '{outside_arg}'; content = 'print(2)' }}
Write-Output ('r:' + (Invoke-Tool 'write_bound_file' $a $folder))
""")
    line = next(ln for ln in out.splitlines() if ln.startswith("r:"))
    assert "Cannot go outside the bound folder" in line
    assert not outside.exists()


def test_normal_relative_path_is_unaffected(tmp_path):
    inference_path = str(ROOT / "agent" / "mamidlabs-inference.ps1").replace("\\", "\\\\")
    folder = str(tmp_path).replace("\\", "\\\\")
    out = run_pwsh_functions(rf"""
$JobData = @{{ root = '{tmp_path}' }}
$Shared = [hashtable]::Synchronized(@{{ AccessMode = 'full'; Cancel = $false }})
Invoke-Expression (Get-Functions '{inference_path}')
$folder = '{folder}'
$a = [pscustomobject]@{{ relative_path = 'normal.py'; content = 'print(3)' }}
Write-Output ('r:' + (Invoke-Tool 'write_bound_file' $a $folder))
""")
    line = next(ln for ln in out.splitlines() if ln.startswith("r:"))
    assert line == "r:Written: normal.py (8 characters)"


def test_run_command_survives_nested_quoted_powershell_command(tmp_path):
    """This used to print the 'if' line as is without running it
    (the nested quotes of a command passed directly as an argument to cmd.exe /c
    got broken). Now the command is written to a temporary .cmd file and run."""
    inference_path = str(ROOT / "agent" / "mamidlabs-inference.ps1").replace("\\", "\\\\")
    folder = str(tmp_path).replace("\\", "\\\\")
    out = run_pwsh_functions(rf"""
$JobData = @{{ root = '{tmp_path}' }}
$Shared = [hashtable]::Synchronized(@{{ AccessMode = 'full'; Cancel = $false }})
Invoke-Expression (Get-Functions '{inference_path}')
$folder = '{folder}'
$a = [pscustomobject]@{{ command = 'powershell -NoProfile -Command "if (1 -eq 1) {{ Write-Host yes }} else {{ Write-Host no }}"' }}
Write-Output ('r:' + (Invoke-Tool 'run_command' $a $folder))
""")
    result = out.split("r:", 1)[1]
    assert "Exit code: 0" in result
    assert "yes" in result
    assert "not recognized" not in result


def test_run_command_percent_sign_is_not_swallowed_by_cmd(tmp_path):
    """A single "%" in a command written to a .cmd file must not be taken by cmd.exe
    for a parameter reference and silently swallowed (e.g. modulo "42 % 2 -eq 0")."""
    inference_path = str(ROOT / "agent" / "mamidlabs-inference.ps1").replace("\\", "\\\\")
    folder = str(tmp_path).replace("\\", "\\\\")
    out = run_pwsh_functions(rf"""
$JobData = @{{ root = '{tmp_path}' }}
$Shared = [hashtable]::Synchronized(@{{ AccessMode = 'full'; Cancel = $false }})
Invoke-Expression (Get-Functions '{inference_path}')
$folder = '{folder}'
$a = [pscustomobject]@{{ command = 'powershell -NoProfile -Command "if (42 % 2 -eq 0) {{ Write-Output even }} else {{ Write-Output odd }}"' }}
Write-Output ('r:' + (Invoke-Tool 'run_command' $a $folder))
""")
    result = out.split("r:", 1)[1]
    assert "Exit code: 0" in result
    assert "even" in result


def test_run_command_percent_env_var_reference_still_expands(tmp_path, monkeypatch):
    """A real (matched, has a closing "%" on the line) environment variable reference like
    "%FOO%" should still expand normally; only an unmatched/lone "%" is escaped (see the
    run_command comment in mamidlabs-inference.ps1 and
    test_run_command_percent_sign_is_not_swallowed_by_cmd)."""
    monkeypatch.setenv("MAMIDLABS_TEST_VAR", "hello-world")
    inference_path = str(ROOT / "agent" / "mamidlabs-inference.ps1").replace("\\", "\\\\")
    folder = str(tmp_path).replace("\\", "\\\\")
    out = run_pwsh_functions(rf"""
$JobData = @{{ root = '{tmp_path}' }}
$Shared = [hashtable]::Synchronized(@{{ AccessMode = 'full'; Cancel = $false }})
Invoke-Expression (Get-Functions '{inference_path}')
$folder = '{folder}'
$a = [pscustomobject]@{{ command = 'echo %MAMIDLABS_TEST_VAR%' }}
Write-Output ('r:' + (Invoke-Tool 'run_command' $a $folder))
""")
    result = out.split("r:", 1)[1]
    assert "Exit code: 0" in result
    assert "hello-world" in result


def test_run_command_non_ascii_characters_survive_temp_script_roundtrip(tmp_path):
    """Writing UTF-8 without BOM plus "chcp 65001" should let non-ASCII characters in the
    command/output survive without corruption (previously [Text.Encoding]::Default produced
    UTF-8 without BOM on pwsh 7, not ANSI, but cmd.exe read the file with its own OEM code
    page so non-ASCII characters could get garbled)."""
    inference_path = str(ROOT / "agent" / "mamidlabs-inference.ps1").replace("\\", "\\\\")
    folder = str(tmp_path).replace("\\", "\\\\")
    out = run_pwsh_functions(rf"""
$JobData = @{{ root = '{tmp_path}' }}
$Shared = [hashtable]::Synchronized(@{{ AccessMode = 'full'; Cancel = $false }})
Invoke-Expression (Get-Functions '{inference_path}')
$folder = '{folder}'
$a = [pscustomobject]@{{ command = 'powershell -NoProfile -Command "Write-Output café-résumé"' }}
Write-Output ('r:' + (Invoke-Tool 'run_command' $a $folder))
""")
    result = out.split("r:", 1)[1]
    assert "Exit code: 0" in result
    assert "café-résumé" in result


def test_run_command_temp_script_is_cleaned_up(tmp_path):
    inference_path = str(ROOT / "agent" / "mamidlabs-inference.ps1").replace("\\", "\\\\")
    folder = str(tmp_path).replace("\\", "\\\\")
    temp_before = set(Path(__import__("tempfile").gettempdir()).glob("mamidlabs-cmd-*.cmd"))
    run_pwsh_functions(rf"""
$JobData = @{{ root = '{tmp_path}' }}
$Shared = [hashtable]::Synchronized(@{{ AccessMode = 'full'; Cancel = $false }})
Invoke-Expression (Get-Functions '{inference_path}')
$folder = '{folder}'
$a = [pscustomobject]@{{ command = 'echo test' }}
Invoke-Tool 'run_command' $a $folder | Out-Null
""")
    temp_after = set(Path(__import__("tempfile").gettempdir()).glob("mamidlabs-cmd-*.cmd"))
    assert temp_after - temp_before == set(), "the temporary .cmd file must be deleted after running"
