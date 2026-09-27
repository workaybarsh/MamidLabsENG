"""The install script must bring an existing ComfyUI copy to the pinned commit.

On a real machine: since ComfyUI\\main.py existed the script skipped ComfyUI without
looking; the 6-day-old 7a0b5ee copy never moved to the pinned b0f4b7b, and the manual
lora.py patch inside it went unnoticed.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


# The install script also runs with Windows PowerShell 5.1 on a clean machine.
@pytest.mark.parametrize("shell", ["pwsh", "powershell"])
def test_existing_checkout_is_moved_to_pinned_commit_and_local_changes_are_backed_up(tmp_path, shell):
    pwsh = shutil.which(shell)
    if not pwsh or not shutil.which("git"):
        pytest.skip(f"{shell} or git not found")

    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git("init", "-q", cwd=upstream)
    git("config", "user.email", "t@t", cwd=upstream)
    git("config", "user.name", "t", cwd=upstream)
    (upstream / "main.py").write_text("v1\n")
    git("add", ".", cwd=upstream)
    git("commit", "-qm", "v1", cwd=upstream)
    old = git("rev-parse", "HEAD", cwd=upstream)
    (upstream / "main.py").write_text("v2\n")
    git("commit", "-qam", "v2", cwd=upstream)
    pinned = git("rev-parse", "HEAD", cwd=upstream)
    # To be able to fetch a specific SHA from a local file:// repository.
    git("config", "uploadpack.allowReachableSHA1InWant", "true", cwd=upstream)

    install_root = tmp_path / "MamidLabs"
    comfy = install_root / "ComfyUI"
    subprocess.run(["git", "clone", "-q", str(upstream), str(comfy)], check=True)
    git("checkout", "-q", old, cwd=comfy)
    (comfy / "main.py").write_text("v1 + manual patch\n")

    script = tmp_path / "sync.ps1"
    script.write_text(
        f"""
$ErrorActionPreference = 'Stop'
$ast = [Management.Automation.Language.Parser]::ParseFile('{ROOT}\\scripts\\install-mamidlabs.ps1', [ref]$null, [ref]$null)
$fn = $ast.FindAll({{ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Sync-PinnedCheckout' }}, $true) | Select-Object -First 1
. ([scriptblock]::Create($fn.Extent.Text))
$script:log = [Collections.ArrayList]::new()
function Write-Warn($t) {{ [void]$script:log.Add('warning: ' + $t) }}
function Write-Ok($t) {{ [void]$script:log.Add('ok: ' + $t) }}
function Write-Note($t) {{ [void]$script:log.Add('note: ' + $t) }}
function Write-Skip($t) {{ [void]$script:log.Add('skip: ' + $t) }}
$Root = '{install_root}'
Sync-PinnedCheckout '{comfy}' '{upstream}' '{pinned}' 'ComfyUI' *> $null
Sync-PinnedCheckout '{comfy}' '{upstream}' '{pinned}' 'ComfyUI' *> $null
@{{ log = @($script:log) }} | ConvertTo-Json -Compress
""",
        encoding="utf-8-sig",
    )
    result = subprocess.run(
        [pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-NonInteractive", "-File", str(script)],
        capture_output=True, encoding="utf-8", errors="replace", check=False, timeout=90,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    log = json.loads(result.stdout.strip().splitlines()[-1])["log"]

    assert git("rev-parse", "HEAD", cwd=comfy) == pinned
    assert (comfy / "main.py").read_text() == "v2\n"
    backups = list((install_root / "tools" / "backup").glob("comfyui-*.patch"))
    assert len(backups) == 1 and "manual patch" in backups[0].read_text(encoding="utf-8")
    assert any(line.startswith("ok: ComfyUI brought to the pinned version") for line in log)
    # Second run: already at the pinned version, skips without touching it.
    assert log[-1].startswith("skip: ComfyUI already at the pinned version")


@pytest.mark.parametrize("shell", ["pwsh", "powershell"])
def test_preflight_runs_cleanly_in_both_powershell_editions(tmp_path, shell):
    """Windows PowerShell 5.1 strips double quotes from native arguments: when the Python
    version check was given with "-c" it failed with a syntax error and produced a wrong
    "3.10 required" warning. Also, with the 'Stop' preference a stderr redirect stopped the script."""
    exe = shutil.which(shell)
    if not exe or not shutil.which("python"):
        pytest.skip(f"{shell} or python not found")
    result = subprocess.run(
        [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts" / "install-mamidlabs.ps1"),
         "-Stage", "preflight", "-Root", str(tmp_path)],
        capture_output=True, encoding="utf-8", errors="replace", check=False, timeout=120,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "SyntaxError" not in output
    assert "[ok]   Python 3." in output, output