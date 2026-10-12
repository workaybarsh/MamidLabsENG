"""Script-based updating (install-mamidlabs.ps1 -Stage layout) must also refresh the wizard
source package ({root}\\setup-source).

A wizard-installed root keeps the program source under `setup-source`, used by Inno's
cancel/rollback. Script-based updating did not refresh that folder, so it stayed at the
previous version; it is now refreshed when present, and not created for a script-based install.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8-sig")


def test_layout_refreshes_existing_wizard_source_package():
    script = read_text("scripts/install-mamidlabs.ps1")
    assert "$setupSource = Join-Path $Root 'setup-source'" in script
    assert "if (Test-Path -LiteralPath $setupSource -PathType Container)" in script
    assert "foreach ($folder in @('agent', 'scripts', 'requirements'))" in script
    assert "Join-Path $Source 'docs\\installation.md'" in script
    assert "Where-Object { $_.Name -ne 'installer' }" in script
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
