"""Source/version/size manifest for components outside the package.

PowerShell 7, llama.cpp and the CUDA runtime are defined in $script:Packages with a
URL and byte size; Invoke-Download verifies the size after download. ComfyUI and its
custom nodes are pinned to a full commit SHA, so installs made on different dates get
the same version (shallow fetch + checkout FETCH_HEAD).
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8-sig")


def test_runtime_packages_carry_source_and_verified_size():
    installer = read_text("scripts/install-mamidlabs.ps1")
    for name in ("pwsh", "llama", "cudart"):
        assert f"{name} = @{{" in installer, f"{name} missing in $script:Packages"
    assert "if ($actual -ne $expectedBytes)" in installer
    assert "The file was deleted, run again." in installer


def test_comfyui_clone_is_pinned_to_a_specific_commit_not_a_floating_branch():
    installer = read_text("scripts/install-mamidlabs.ps1")
    assert "$script:ComfyCommit = '" in installer
    # It must be a full 40-character git SHA, not a short/ambiguous reference.
    import re
    match = re.search(r"\$script:ComfyCommit = '([0-9a-f]+)'", installer)
    assert match, "ComfyCommit definition not found"
    assert len(match.group(1)) == 40, "a short/ambiguous commit reference must not be used"
    # The old branch-bound clone pattern must be gone.
    assert "git clone --depth 1 $script:ComfyRepo $target" not in installer
    assert "fetch --depth 1 origin $script:ComfyCommit" in installer
    assert "checkout -q FETCH_HEAD" in installer
