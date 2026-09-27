"""requirements/*.txt is the single source of Python dependencies.

The install script reads its package lists from these files; versions are pinned and
requirements/app.txt must match the real imports in agent/*.py.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8-sig")


def parse_requirements(name: str) -> list[str]:
    lines = (ROOT / "requirements" / name).read_text(encoding="utf-8").splitlines()
    return [line.split("#", 1)[0].strip() for line in lines if line.split("#", 1)[0].strip()]


def package_name(pin: str) -> str:
    """'requests==2.34.2' -> 'requests'."""
    return re.split(r"[=<>!~]", pin, 1)[0].strip()


def test_requirements_files_exist_and_are_not_empty():
    for name in ("app.txt", "imagegen.txt"):
        packages = parse_requirements(name)
        assert packages, f"requirements/{name} must not be empty"


def test_requirements_are_version_pinned():
    """Without pinned versions, installs made on different dates could get different
    package versions."""
    for name in ("app.txt", "imagegen.txt"):
        for pin in parse_requirements(name):
            assert "==" in pin, f"requirements/{name}: '{pin}' is not pinned to a version"


def test_app_requirements_cover_every_agent_import():
    """The actual imports in agent/*.py and requirements/app.txt must be the same;
    if one is updated before the other the installation silently leaves a package missing."""
    packages = {package_name(p) for p in parse_requirements("app.txt")}
    # Module name -> PyPI package name (those that are not identical).
    expected = {
        "requests", "pillow", "huggingface_hub", "ddgs", "pypdf",
        "reportlab", "python-docx", "python-pptx", "openpyxl", "rarfile",
    }
    assert packages == expected


def test_imagegen_requirements_exclude_torch():
    """torch/torchvision/torchaudio are installed separately from the CUDA 13.0 index;
    if they were in requirements/imagegen.txt pip could pull the default CPU wheel and
    silently disable GPU acceleration."""
    packages = [package_name(p) for p in parse_requirements("imagegen.txt")]
    assert "av" in packages
    assert not any("torch" in p for p in packages)


def test_installer_reads_packages_from_requirements_files():
    installer = read_text("scripts/install-mamidlabs.ps1")

    assert "function Read-Requirements" in installer
    assert "$script:AppPackages = Read-Requirements 'app.txt'" in installer
    assert "$script:ImagegenPackages = Read-Requirements 'imagegen.txt'" in installer
    # With a missing file it must not silently continue with an empty list.
    assert "throw ('Requirements file not found: ' + $path)" in installer
    # A file that is not found must now give an explicit error; the old inline array must be gone.
    assert "$script:AppPackages = @(" not in installer
