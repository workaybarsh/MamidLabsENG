"""MamidLabs local web management (agent/web-server.ps1, web-settings.ps1, scripts/web-serve.ps1).

OpenCode's server model is the reference: localhost-only by default, password-protected
session cookie, single-use short-lived connect link, path confinement for file access.
These tests run functions with a real pwsh (no network listener) and check the security
contracts statically.
"""
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
    server = str(ROOT / "agent" / "web-server.ps1").replace("\\", "\\\\")
    script = rf"""
$ErrorActionPreference = 'Stop'
function Get-Functions([string]$path) {{
    $tokens=$null; $errs=$null
    $ast=[Management.Automation.Language.Parser]::ParseFile($path,[ref]$tokens,[ref]$errs)
    if ($errs.Count) {{ throw ($errs | Out-String) }}
    return (($ast.EndBlock.Statements | Where-Object {{ $_ -is [Management.Automation.Language.FunctionDefinitionAst] }} | ForEach-Object {{ $_.Extent.Text }}) -join "`n")
}}
Invoke-Expression (Get-Functions '{server}')
{body}
"""
    proc = subprocess.run([pwsh, "-NoProfile", "-Command", script], capture_output=True, text=True,
                          timeout=30, encoding="utf-8", errors="replace")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


def test_web_server_and_launcher_files_exist():
    assert (ROOT / "agent" / "web-server.ps1").is_file()
    assert (ROOT / "agent" / "web-settings.ps1").is_file()
    assert (ROOT / "scripts" / "web-serve.ps1").is_file()


def test_default_bind_is_localhost_only_and_lan_is_opt_in():
    server = read_text("agent/web-server.ps1")
    assert "[switch]$Lan" in server
    assert "$prefixHost = if ($Lan) { '+' } else { '127.0.0.1' }" in server


def test_password_is_never_passed_on_the_command_line():
    server = read_text("agent/web-server.ps1")
    assert "$SettingsPasswordField = 'web.password'" in server
    assert "ConvertTo-SecureString" in server
    settings = read_text("agent/web-settings.ps1")
    assert "$script:apiSettings.web.password = (ConvertFrom-SecureString" in settings
    assert "-Password" not in settings


def test_auth_cookie_is_httponly_samesite_and_session_is_30_days():
    server = read_text("agent/web-server.ps1")
    assert "$script:sessionDays = 30" in server
    assert "HttpOnly; SameSite=Strict" in server
    assert "$script:sessionDays * 24 * 3600" in server


def test_pair_link_is_single_use_and_expires_in_five_minutes():
    server = read_text("agent/web-server.ps1")
    assert "$script:pairMinutes = 5" in server
    assert "function New-PairToken" in server
    assert "function Consume-PairToken" in server
    assert "$script:pairs.Remove($token)" in server
    assert "/auth/connect" in server


def test_login_lockout_after_failed_attempts():
    server = read_text("agent/web-server.ps1")
    assert "$script:failedLogins -ge 5" in server
    assert "$script:lockUntil" in server
    assert "429" in server


def test_output_path_traversal_is_blocked():
    server = read_text("agent/web-server.ps1")
    assert "function Resolve-OutputPath" in server
    assert "$safe -match '\\.\\.'" in server
    assert "IsPathRooted" in server
    assert "StartsWith($rootFull" in server


def test_password_comparison_runs_in_real_pwsh():
    out = run_ps(r"""
$script:password = 'secret123'
Write-Output ('good:' + (Test-Password 'secret123' 'secret123'))
Write-Output ('bad:' + (Test-Password 'other' 'secret123'))
Write-Output ('empty:' + (Test-Password '' 'secret123'))
""")
    assert "good:True" in out
    assert "bad:False" in out
    assert "empty:False" in out


def test_pair_token_is_single_use_in_real_pwsh():
    out = run_ps(r"""
$script:pairs = @{}
$t = New-PairToken
Write-Output ('first:' + (Consume-PairToken $t))
Write-Output ('second:' + (Consume-PairToken $t))
Write-Output ('invalid:' + (Consume-PairToken 'none'))
""")
    assert "first:True" in out
    assert "second:False" in out
    assert "invalid:False" in out


def test_session_expiry_is_enforced_in_real_pwsh():
    out = run_ps(r"""
$script:sessions = @{}
$t = New-Session
Write-Output ('valid:' + (Test-Session $t))
$script:sessions[$t] = [DateTime]::UtcNow.AddSeconds(-1)
Write-Output ('expired:' + (Test-Session $t))
""")
    assert "valid:True" in out
    assert "expired:False" in out


def test_model_switch_only_allows_known_ids():
    server = read_text("agent/web-server.ps1")
    assert "builtin-max" in server and "builtin-flash" in server and "builtin-lite" in server
    assert "Invalid model." in server


def test_web_settings_is_wired_into_the_desktop_ui():
    desktop = read_text("qwen-desktop.ps1")
    assert "web-settings.ps1" in desktop
    assert "Show-WebSettings" in desktop
    assert "'Web management…'" in desktop
