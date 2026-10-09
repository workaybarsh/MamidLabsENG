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
