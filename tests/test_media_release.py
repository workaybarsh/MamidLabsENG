"""Language-model reload safety after media (agent/media-model-swap.ps1).

Even when ComfyUI /free unloads its torch cache, the CUDA context and driver-side reservation
may hold VRAM for a while. Looking only at the torch counter could let the language model spill
into shared memory and drop to ~6 tok/s. Release-Comfy now also verifies the real free VRAM and
closes ComfyUI when it is not enough.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8-sig")


def test_release_comfy_verifies_real_free_vram_before_returning():
    swap = read_text("agent/media-model-swap.ps1")
    assert "[int]$minFreeMiB = 0" in swap
    assert "function Get-MediaFreeVramMiB" in swap
    assert "--query-gpu=memory.free" in swap
    assert "if ($null -eq $free -or $free -ge $minFreeMiB) { return }" in swap
    assert "$process.Kill($true)" in swap


def test_image_and_video_release_require_enough_vram_for_the_language_model():
    image = read_text("agent/imagegen-run.ps1")
    video = read_text("agent/videogen-run.ps1")
    for script in (image, video):
        assert "Release-Comfy $comfy $comfyUrl $(if ($lite) { 6000 } else { 14000 })" in script


def test_media_free_vram_helper_returns_null_when_no_nvidia():
    swap = read_text("agent/media-model-swap.ps1")
    helper = swap.split("function Get-MediaFreeVramMiB")[1].split("function Release-Comfy")[0]
    assert "if (!$smi) { return $null }" in helper
