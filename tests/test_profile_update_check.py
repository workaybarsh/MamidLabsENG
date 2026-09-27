"""The "Check for updates" button in the Profiles window.

The button sits next to "Switch to this profile" and checks GitHub's latest release.
If an update is found it turns into "Update now" (one more click downloads it and starts
the setup file); if not, it shows "Up to date" for a few seconds and then resets itself
with a Timer.

Test-MamidLabsUpdateAvailable and Get-InstalledMamidLabsVersion are independent
functions run with real pwsh; the WinForms button/Timer wiring is only checked
statically (in the profile-catalog.ps1 text) since ShowDialog needs a real Windows GUI.
"""
import re
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
    proc = subprocess.run([pwsh, "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=30,
                          encoding="utf-8", errors="replace")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


def test_update_available_when_remote_tag_is_newer_than_local():
    out = run_ps(r"""
function Get-InstalledMamidLabsVersion { '1.0.0' }
function Invoke-RestMethod {
    [pscustomobject]@{ tag_name = 'v1.1.0'; assets = @([pscustomobject]@{ name = 'MamidLabs_Setup.exe'; browser_download_url = 'https://example.com/x.exe'; size = 12345 }) }
}
$r = Test-MamidLabsUpdateAvailable
Write-Output ('available:' + $r.available)
Write-Output ('version:' + $r.version)
Write-Output ('url:' + $r.downloadUrl)
Write-Output ('size:' + $r.size)
""")
    assert "available:True" in out
    assert "version:1.1.0" in out
    assert "url:https://example.com/x.exe" in out
    assert "size:12345" in out


def test_no_update_when_remote_tag_matches_local():
    out = run_ps(r"""
function Get-InstalledMamidLabsVersion { '1.0.0' }
function Invoke-RestMethod {
    [pscustomobject]@{ tag_name = 'v1.0.0'; assets = @([pscustomobject]@{ name = 'MamidLabs_Setup.exe'; browser_download_url = 'https://example.com/x.exe'; size = 1 }) }
}
Write-Output ('available:' + (Test-MamidLabsUpdateAvailable).available)
""")
    assert "available:False" in out


def test_network_failure_reports_an_error_instead_of_throwing():
    out = run_ps(r"""
function Get-InstalledMamidLabsVersion { $null }
function Invoke-RestMethod { throw 'no network' }
$r = Test-MamidLabsUpdateAvailable
Write-Output ('available:' + $r.available)
Write-Output ('hasError:' + [bool]$r.error)
""")
    assert "available:False" in out
    assert "hasError:True" in out


def test_missing_setup_asset_is_reported_without_throwing():
    out = run_ps(r"""
function Get-InstalledMamidLabsVersion { '1.0.0' }
function Invoke-RestMethod {
    [pscustomobject]@{ tag_name = 'v1.1.0'; assets = @([pscustomobject]@{ name = 'source.zip'; browser_download_url = 'https://example.com/x.zip'; size = 1 }) }
}
$r = Test-MamidLabsUpdateAvailable
Write-Output ('available:' + $r.available)
Write-Output ('hasError:' + [bool]$r.error)
""")
    assert "available:False" in out
    assert "hasError:True" in out


def test_unknown_local_version_assumes_an_update_is_available():
    """If no registry entry is found (maybe installed outside the wizard), fail safe:
    assume there is an update and let the user see "Update now" and decide - better
    than silently saying "up to date" and leaving them on an old version."""
    out = run_ps(r"""
function Get-InstalledMamidLabsVersion { $null }
function Invoke-RestMethod {
    [pscustomobject]@{ tag_name = 'v9.9.9'; assets = @([pscustomobject]@{ name = 'MamidLabs_Setup.exe'; browser_download_url = 'https://example.com/x.exe'; size = 1 }) }
}
Write-Output ('available:' + (Test-MamidLabsUpdateAvailable).available)
""")
    assert "available:True" in out


def test_update_check_uses_this_editions_own_app_id_and_repository():
    """TR and ENG have separate AppIds; if the update check looked at the wrong key it
    would never find the local version and would offer an update every time."""
    catalog = read_text("agent/profile-catalog.ps1")
    iss = read_text("packaging/windows/mamidlabs.iss")
    iss_app_id = re.search(r"AppId=\{\{([0-9A-Fa-f-]+)\}", iss).group(1)
    catalog_app_id = re.search(r"\$script:MamidLabsAppId = '\{([0-9A-Fa-f-]+)\}'", catalog).group(1)
    assert iss_app_id == catalog_app_id
    assert "workaybarsh/MamidLabsENG" in catalog
    assert "releases/latest" in catalog


def test_profile_manager_wires_up_the_three_state_update_button():
    catalog = read_text("agent/profile-catalog.ps1")
    assert "$updateBtn = [Windows.Forms.Button]::new()" in catalog
    assert "'Check for updates'" in catalog
    assert "'Update now'" in catalog
    assert "'Up to date'" in catalog
    # Resets itself with a Timer if there is no update / on error (as if never clicked).
    assert "[Windows.Forms.Timer]::new()" in catalog
    assert "$updateResetTimer.Start()" in catalog
    # 'Update now' downloads and starts the setup file.
    assert "Start-Process -FilePath $downloadPath" in catalog
    assert "Test-MamidLabsUpdateAvailable" in catalog
    # The button sits next to the existing "Switch to this profile" button.
    assert catalog.index("$switchBtn.SetBounds") < catalog.index("$updateBtn.SetBounds") < catalog.index("$closeBtn.SetBounds")


def test_update_closes_the_app_after_launching_the_installer():
    """If the app stays open, install-mamidlabs.ps1 stops at the 'layout' stage with
    "MamidLabs is open" and the update never completes. Once the setup wizard has
    started, the main window must close (FormClosing saves the chat, stops the servers)."""
    catalog = read_text("agent/profile-catalog.ps1")
    handler = catalog[catalog.index("$updateBtn.Add_Click"):]
    assert "$mainForm = $form" in catalog
    assert handler.index("Start-Process -FilePath $downloadPath") < handler.index("$mainForm.Close()")
    installer = read_text("scripts/install-mamidlabs.ps1")
    assert "MamidLabs is open; the program files are locked" in installer
