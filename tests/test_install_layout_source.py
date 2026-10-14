"""Script-based updating (install-mamidlabs.ps1 -Stage layout) must also refresh the wizard
source package ({root}\\setup-source).

A wizard-installed root keeps the program source under `setup-source`, used by Inno's
cancel/rollback. Script-based updating did not refresh that folder, so it stayed at the
previous version; it is now refreshed when present, and not created for a script-based install.
"""
from pathlib import Path

import re
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8-sig")


def run_installer_ps(body: str, timeout: int = 60) -> str:
    """Run `body` in a real pwsh with the functions of install-mamidlabs.ps1 loaded.

    Mirrors tests/conftest.py: without PowerShell 7 the test is skipped, not passed.
    """
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")
    path = str(ROOT / "scripts" / "install-mamidlabs.ps1").replace("\\", "\\\\")
    script = rf"""
$ErrorActionPreference = 'Stop'
function Get-Functions([string]$path) {{
    $tokens=$null; $errs=$null
    $ast=[Management.Automation.Language.Parser]::ParseFile($path,[ref]$tokens,[ref]$errs)
    if ($errs.Count) {{ throw ($errs | Out-String) }}
    return (($ast.EndBlock.Statements | Where-Object {{ $_ -is [Management.Automation.Language.FunctionDefinitionAst] }} | ForEach-Object {{ $_.Extent.Text }}) -join "`n")
}}
Invoke-Expression (Get-Functions '{path}')
{body}
"""
    proc = subprocess.run([pwsh, "-NoProfile", "-Command", script], capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


def test_layout_refreshes_existing_wizard_source_package():
    script = read_text("scripts/install-mamidlabs.ps1")
    assert "$setupSource = Join-Path $Root 'setup-source'" in script
    # Refreshed only when present AND the source is not the setup-source itself (the wizard
    # runs with -Source {root}\setup-source; a self-copy would empty the package).
    assert "if ((Test-Path -LiteralPath $setupSource -PathType Container) -and ($sourceFull -ine $setupSourceFull))" in script
    assert "foreach ($folder in @('agent', 'scripts', 'requirements'))" in script
    assert "Join-Path $Source 'docs\\installation.md'" in script
    # assets\installer is copied too (installed program files must match the repo).
    assert "Get-ChildItem -LiteralPath $assets -Force | ForEach-Object" in script
    assert "Where-Object { $_.Name -ne 'installer' }" not in script
    assert "Setup source package refreshed" in script


def test_layout_creates_source_package_only_when_it_exists():
    script = read_text("scripts/install-mamidlabs.ps1")
    start = script.index("$setupSource = Join-Path $Root 'setup-source'")
    end = script.index("Setup source package refreshed")
    block = script[start:end]
    assert "Test-Path -LiteralPath $setupSource -PathType Container" in block
    assert "New-Item -ItemType Directory -Path $setupSource" not in block


def test_layout_copies_eula_to_root_and_source_package():
    """EULA.txt is shown on the wizard's license page and the .iss [Files] puts it into the
    source package; a script-based update must copy it both to the root and to setup-source.
    Otherwise the EULA only exists in a wizard install and is lost on a script update."""
    script = read_text("scripts/install-mamidlabs.ps1")
    # Both top-level file lists (root + setup-source) must contain EULA.txt.
    assert script.count("'LICENSE', 'EULA.txt')") == 2
    # The .iss must match the same set.
    iss = read_text("packaging/windows/mamidlabs.iss")
    assert 'Source: "..\\..\\EULA.txt"; DestDir: "{app}\\setup-source"' in iss


def test_layout_creates_and_copies_requirements_into_the_root():
    """`install-mamidlabs.ps1 -Stage layout` must work with the default source: requirements\\
    is created in the root and copied there (previously it was missing from both lists)."""
    script = read_text("scripts/install-mamidlabs.ps1")
    assert "'runtime', 'tools', 'requirements'," in script
    assert "foreach ($folder in @('agent', 'scripts', 'assets', 'requirements'))" in script


def test_layout_prunes_owned_folders_before_copying():
    """Files removed/renamed in the source must not linger in the install root: the owned
    program folders are pruned before the copy. User data is never pruned here."""
    script = read_text("scripts/install-mamidlabs.ps1")
    start = script.index("foreach ($folder in @('agent', 'scripts', 'assets', 'requirements'))")
    block = script[start:script.index("Program files copied", start)]
    assert "if (Test-Path -LiteralPath $to) { Remove-Item -LiteralPath $to -Recurse -Force }" in block
    assert "Copy-Item -LiteralPath $from -Destination $Root -Recurse -Force" in block
    for keep in ("settings", "chats", "models", "logs", "outputs", "tools", "venv", "ComfyUI"):
        assert "'%s'" % keep not in block, keep


def test_source_package_refresh_prunes_and_applies_the_same_excludes():
    script = read_text("scripts/install-mamidlabs.ps1")
    start = script.index("$setupSource = Join-Path $Root 'setup-source'")
    block = script[start:script.index("Setup source package refreshed", start)]
    assert "if (Test-Path -LiteralPath $to) { Remove-Item -LiteralPath $to -Recurse -Force }" in block
    assert "Remove-ExcludedFromOwned $to $folder" in block


def test_layout_applies_the_iss_excludes_to_the_script_copy():
    """The .iss excludes agent\\__pycache__\\*, *.pyc (assets\\installer\\* is included now, so the
    installed program files match the repo); the script copy drops the same paths."""
    script = read_text("scripts/install-mamidlabs.ps1")
    iss = read_text("packaging/windows/mamidlabs.iss")
    assert 'Excludes: "__pycache__\\*,*.pyc"' in iss
    assert 'Excludes: "installer\\*"' not in iss
    assert "Remove-ExcludedFromOwned $to $folder" in script
    assert "-Filter '__pycache__'" in script and "-Filter '*.pyc'" in script
    assert "$installer = Join-Path $path 'installer'" not in script


def test_layout_writes_display_version_into_the_script_uninstall_entry():
    """The script install's uninstall key needs DisplayVersion, otherwise the in-app update
    check cannot read the local version (see agent/profile-catalog.ps1). Only a strict x.y.z
    value is read from the .iss: the updater parses it as a [version]."""
    script = read_text("scripts/install-mamidlabs.ps1")
    assert "[string]$AppVersion" in script
    assert "$values['DisplayVersion'] = $appVersion" in script
    assert "Get-AppVersionFromSource $Source" in script
    # The single source of the version is the .iss MyAppVersion; only MAJOR.MINOR.PATCH.
    assert 'MyAppVersion\\s+"(\\d+\\.\\d+\\.\\d+)"' in script
    assert 'MyAppVersion\\s+"([^"]+)"' not in script


def test_layout_skips_a_duplicate_uninstall_entry_for_a_wizard_install():
    """A repeated `layout` must not add a second Apps entry next to the wizard's own
    ({AppId}_is1) key; the installer's AppId must match the .iss."""
    script = read_text("scripts/install-mamidlabs.ps1")
    assert "if (Test-InnoUninstallEntryExists $Root)" in script
    assert "$script:MamidLabsAppId" in script
    iss = read_text("packaging/windows/mamidlabs.iss")
    iss_app_id = re.search(r"AppId=\{\{([0-9A-Fa-f-]+)\}", iss).group(1)
    installer_app_id = re.search(r"\$script:MamidLabsAppId = '\{([0-9A-Fa-f-]+)\}'", script).group(1)
    assert iss_app_id == installer_app_id


def test_source_package_refresh_is_skipped_when_source_is_the_setup_source():
    """The wizard runs `-Stage layout` with `-Source {root}\\setup-source`. Refreshing the
    source package from itself would copy every top-level file/folder onto itself
    ("cannot overwrite itself") and empty the package, so the refresh is skipped in that
    case. A source merely inside the root must still copy the program files into the root,
    so the `$sameRoot` guard stays equality-only."""
    script = read_text("scripts/install-mamidlabs.ps1")
    # Equality-only guard (no "contained in root" test).
    assert "$sameRoot = [IO.Path]::GetFullPath($Source).TrimEnd('\\') -ieq [IO.Path]::GetFullPath($Root).TrimEnd('\\')" in script
    assert "$sameRoot.StartsWith" not in script
    # The refresh is skipped when Source == {root}\\setup-source.
    start = script.index("$setupSource = Join-Path $Root 'setup-source'")
    block = script[start:script.index("Setup source package refreshed", start)]
    assert "$sourceFull -ine $setupSourceFull" in block
    assert "$setupSourceFull = [IO.Path]::GetFullPath($setupSource).TrimEnd('\\')" in block


def test_get_app_version_from_source_accepts_only_semver(tmp_path):
    """Only a strict x.y.z MyAppVersion is returned. The in-app updater parses DisplayVersion
    as a [version], so a non-x.y.z value (partial/extra, placeholder) must be ignored and no
    DisplayVersion written at all, instead of a value that breaks the comparison."""
    src = tmp_path / "src"
    (src / "packaging" / "windows").mkdir(parents=True)
    iss = src / "packaging" / "windows" / "mamidlabs.iss"
    iss.write_text('; version\n#define MyAppVersion "2.3.4"\n', encoding="utf-8")
    out = run_installer_ps(rf"$Source = '{src}'; Write-Output ('v:' + (Get-AppVersionFromSource $Source))")
    assert "v:2.3.4" in out
    # A non-x.y.z value must be ignored.
    iss.write_text('; version\n#define MyAppVersion "1.2"\n', encoding="utf-8")
    out = run_installer_ps(rf"$Source = '{src}'; Write-Output ('v:' + (Get-AppVersionFromSource $Source))")
    assert "v:1.2" not in out
    # Four parts are not x.y.z either.
    iss.write_text('; version\n#define MyAppVersion "1.2.3.4"\n', encoding="utf-8")
    out = run_installer_ps(rf"$Source = '{src}'; Write-Output ('v:' + (Get-AppVersionFromSource $Source))")
    assert "v:1.2.3.4" not in out


def test_layout_from_install_root_without_source_does_not_self_copy(tmp_path):
    """The documented command runs from an installed copy without `-Source`, so Source == Root;
    copying the program files onto themselves would fail ("cannot overwrite itself"). The run
    is skipped and succeeds (requirements\\ is in the root so Read-Requirements does not stop)."""
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")
    root = tmp_path / "MamidLabsRoot"
    req = root / "requirements"
    req.mkdir(parents=True)
    (req / "app.txt").write_text("pytest", encoding="utf-8")
    (req / "imagegen.txt").write_text("", encoding="utf-8")
    (req / "asr.txt").write_text("", encoding="utf-8")
    proc = subprocess.run(
        [pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(ROOT / "scripts" / "install-mamidlabs.ps1"),
         "-Root", str(root), "-Source", str(root), "-Stage", "layout", "-NoUninstallEntry"],
        capture_output=True, text=True, timeout=180, encoding="utf-8", errors="replace")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (req / "app.txt").exists()


def test_layout_from_wizard_nested_source_copies_program_files(tmp_path):
    """The wizard extracts the source package under {root}\\setup-source and runs the installer
    as `-Root {root} -Source {root}\\setup-source`. Although the source is INSIDE the root, the
    program files must still be copied into the root (a containment check would wrongly treat
    them as "already in place" and leave a fresh install empty). The nested source package must
    not be copied onto itself either."""
    if sys.platform != "win32":
        pytest.skip("Windows installer layout")
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")
    root = tmp_path / "MamidLabsWiz"
    src = root / "setup-source"
    # Read-Requirements reads $Source\requirements at script load time.
    req = src / "requirements"
    req.mkdir(parents=True)
    for name in ("app.txt", "imagegen.txt", "asr.txt"):
        (req / name).write_text("", encoding="utf-8")
    for folder in ("agent", "scripts", "assets", "requirements"):
        (src / folder).mkdir(parents=True, exist_ok=True)
        (src / folder / "probe-marker.txt").write_text("probe", encoding="utf-8")
    proc = subprocess.run(
        [pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(ROOT / "scripts" / "install-mamidlabs.ps1"),
         "-Root", str(root), "-Source", str(src), "-Stage", "layout", "-NoUninstallEntry"],
        capture_output=True, text=True, timeout=180, encoding="utf-8", errors="replace")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Program files copied ->" in proc.stdout
    # All four owned program folders are copied into the root.
    for folder in ("agent", "scripts", "assets", "requirements"):
        assert (root / folder / "probe-marker.txt").exists(), proc.stdout + proc.stderr
    # The source package itself is not emptied by a self-copy.
    assert (src / "agent" / "probe-marker.txt").exists(), proc.stdout + proc.stderr
