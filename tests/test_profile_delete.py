"""The "Delete profile" button in the Profile manager, and shared-file protection.

Deleting a profile removes ONLY files unique to it; files shared with other profiles
(e.g. LITE's media files also used by MAX/FLASH) are kept. MAX and FLASH use the exact
same file set, so they are treated as aliases, not as sharing.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8-sig")


def run_ps(body: str) -> str:
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")
    catalog_path = str(ROOT / "agent" / "profile-catalog.ps1").replace("\\", "\\\\")
    script = rf"""
$ErrorActionPreference = 'Stop'
function Get-Functions([string]$path) {{
    $tokens=$null; $errs=$null
    $ast=[Management.Automation.Language.Parser]::ParseFile($path,[ref]$tokens,[ref]$errs)
    if ($errs.Count) {{ throw ($errs | Out-String) }}
    return (($ast.EndBlock.Statements | Where-Object {{ $_ -is [Management.Automation.Language.FunctionDefinitionAst] }} | ForEach-Object {{ $_.Extent.Text }}) -join "`n")
}}
Invoke-Expression (Get-Functions '{catalog_path}')
{body}
"""
    proc = subprocess.run([pwsh, "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=60,
                          encoding="utf-8", errors="replace")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


def make_catalog(tmp_path: Path) -> Path:
    agent = tmp_path / "agent"
    agent.mkdir()
    profiles = {
        "profiles": [
            {"id": "a", "label": "A", "files": [
                {"target": "models/t1.gguf", "repo": "x/y", "bytes": 1},
                {"target": "models/t2.gguf", "repo": "x/y", "bytes": 1}]},
            {"id": "c", "label": "C", "files": [
                {"target": "models/t1.gguf", "repo": "x/y", "bytes": 1},
                {"target": "models/t2.gguf", "repo": "x/y", "bytes": 1}]},
            {"id": "b", "label": "B", "files": [
                {"target": "models/t2.gguf", "repo": "x/y", "bytes": 1},
                {"target": "models/t3.gguf", "repo": "x/y", "bytes": 1}]},
        ]
    }
    (agent / "profiles.json").write_text(json.dumps(profiles), encoding="utf-8")
    (tmp_path / "models").mkdir()
    for name in ("t1", "t2", "t3"):
        (tmp_path / "models" / f"{name}.gguf").write_bytes(b"x")
    return tmp_path


def test_profile_file_component_classification():
    """Component filter: ComfyUI -> media, venv\\asr and whisper models -> audio,
    the rest (LM/mmproj) -> core."""
    out = run_ps(r"""
'ComfyUI/models/unet/x.gguf' | ForEach-Object { Get-ProfileFileComponent $_ }
'ComfyUI\\models\\vae\\y.safetensors' | ForEach-Object { Get-ProfileFileComponent $_ }
'models/whisper-large-v3/model.bin' | ForEach-Object { Get-ProfileFileComponent $_ }
'venv/asr/Scripts/python.exe' | ForEach-Object { Get-ProfileFileComponent $_ }
'models/Huihui-Qwen3.5-9B/x.gguf' | ForEach-Object { Get-ProfileFileComponent $_ }
""")
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    assert lines == ["media", "media", "audio", "audio", "core"]


def test_unique_targets_exclude_shared_files_but_keep_aliases(tmp_path):
    make_catalog(tmp_path)
    root = str(tmp_path).replace("\\", "\\\\")
    out = run_ps(rf"""
$root = '{root}'
$script:profileCatalog = $null
Write-Output ('uniqueA:' + ((@(Get-ProfileUniqueTargets 'a') | Sort-Object) -join ','))
Write-Output ('uniqueB:' + ((@(Get-ProfileUniqueTargets 'b') | Sort-Object) -join ','))
""")
    assert "uniqueA:models/t1.gguf" in out
    assert "uniqueB:models/t3.gguf" in out


def test_remove_profile_files_deletes_only_unique_files(tmp_path):
    make_catalog(tmp_path)
    root = str(tmp_path).replace("\\", "\\\\")
    out = run_ps(rf"""
$root = '{root}'
$script:profileCatalog = $null
$r = Remove-ProfileFiles 'a'
Write-Output ('removed:' + $r.removed)
Write-Output ('failed:' + $r.failed)
Write-Output ('t1:' + (Test-Path -LiteralPath (Join-Path $root 'models/t1.gguf')))
Write-Output ('t2:' + (Test-Path -LiteralPath (Join-Path $root 'models/t2.gguf')))
Write-Output ('t3:' + (Test-Path -LiteralPath (Join-Path $root 'models/t3.gguf')))
""")
    assert "removed:1" in out
    assert "failed:0" in out
    assert "t1:False" in out
    assert "t2:True" in out
    assert "t3:True" in out


def test_profile_manager_wires_up_a_delete_button():
    catalog = read_text("agent/profile-catalog.ps1")
    assert "$deleteBtn = [Windows.Forms.Button]::new()" in catalog
    assert "'Delete profile'" in catalog
    assert "Get-ProfileUniqueTargets" in catalog
    assert "Remove-ProfileFiles" in catalog
    assert "Get-ProfileUniqueTargets $profile.id" in catalog
    add_range = catalog.split("$dlg.Controls.AddRange")[1]
    assert "$deleteBtn" in add_range
