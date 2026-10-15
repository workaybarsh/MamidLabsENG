import json
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8-sig")


def extract_function(source: str, name: str) -> str:
    start = source.index(f"function {name}")
    end = source.find("\nfunction ", start + 1)
    return source[start:end if end != -1 else len(source)]


def write_safetensors(path: Path, tensors: dict, metadata: dict) -> bytes:
    header = {"__metadata__": metadata}
    data = b""
    for key, raw in tensors.items():
        header[key] = {"dtype": "F32", "shape": [len(raw) // 4], "data_offsets": [len(data), len(data) + len(raw)]}
        data += raw
    encoded = json.dumps(header).encode("utf-8")
    path.write_bytes(struct.pack("<Q", len(encoded)) + encoded + data)
    return data


def read_safetensors(path: Path):
    blob = path.read_bytes()
    length = struct.unpack("<Q", blob[:8])[0]
    return length, json.loads(blob[8:8 + length]), blob[8 + length:]


def test_comfy_is_reused_and_released_instead_of_restarted():
    """Starting ComfyUI from scratch for every job took ~20 s (measured on real
    hardware, VRAM sat idle during that time). After a successful job the process
    is not closed, the models are unloaded with /free; image and video share the same
    process, otherwise a second process would clash on the same port."""
    swap = read_text("agent/media-model-swap.ps1")
    assert "function Start-OrReuseComfy" in swap
    assert "function Release-Comfy" in swap
    assert '"unload_models":true,"free_memory":true' in swap
    assert "if ($lite) { $comfyArgs += '--lowvram' }" in swap

    for path in ("agent/imagegen-run.ps1", "agent/videogen-run.ps1"):
        script = read_text(path)
        assert "Start-Process -FilePath $python" not in script, path
        assert "Release-Comfy $comfy $comfyUrl" in script, path
        # On an error during startup the half-open process must be killed too.
        assert "Stop-Comfy $(if ($comfy) { $comfy } else { $Shared.Process })" in script, path


REALISM_CASES = [
    ("Generate a photo of a dog running on the beach at sunset.", False),
    ("Draw a wooden cabin in a snowy mountain village", False),
    ("Draw a cat in anime style", False),
    ("Generate a realistic portrait", True),
    ("A REALISTIC car image", True),
    ("a photorealistic kitchen", True),
    ("Make this more realistic", True),
    ("a forest that looks true to life", True),
    ("make it look like a real photo", True),
    ("a landscape in photo quality", True),
    ("a photorealistic portrait of a sailor", True),
    ("realistic lighting on a city street", True),
    ("It should not be realistic, make it like a cartoon", False),
    ("not realistic, watercolor style", False),
    ("an unrealistic dreamlike castle", False),
    ("surrealistic melting clocks", False),
    ("not realistic, cartoon style", False),
]


def test_realism_lora_is_enabled_only_when_user_asks_for_realism(tmp_path):
    """The realism LoRA slows generation down ~2x on an 8 GB card; it is turned on
    only when the user's own message asks for a realistic image. The decision is
    not left to the language model (the realism parameter was removed from the tool)."""
    inference = read_text("agent/mamidlabs-inference.ps1")
    assert "realism = (Test-RealismIntent $userText)" in inference
    assert "realism = @{ type = 'boolean'" not in inference
    assert "$realism = [bool]$JobData.realism" in read_text("agent/imagegen-run.ps1")

    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh not found")
    function = extract_function(inference, "Test-RealismIntent")
    cases = "\n".join(
        "Write-Output ([string](Test-RealismIntent '{}'))".format(text.replace("'", "''")) for text, _ in REALISM_CASES
    )
    script = tmp_path / "realism.ps1"
    script.write_text(function + "\n" + cases + "\n", encoding="utf-8-sig")
    result = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-File", str(script)],
        capture_output=True, encoding="utf-8", errors="replace", check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    got = [line.strip() == "True" for line in result.stdout.splitlines() if line.strip()]
    assert got == [expected for _, expected in REALISM_CASES], list(zip(REALISM_CASES, got))


# (text, is there an image in the chat/request, expected)
IMAGE_REQUEST_CASES = [
    ("What's the weather forecast for Istanbul this weekend? Give a short summary.", False, False),
    ("What is the EUR/USD exchange rate today?", False, False),
    ("Create a Word document for this meeting", False, False),
    ("Generate an illustration of a futuristic city with neon lights", False, True),
    ("Draw a cat", False, True),
    ("Design a minimal logo for my company", False, True),
    ("Generate a snowy mountain landscape", False, True),
    ("Make me a picture", False, True),
    ("create a photo of a dog", False, True),
    ("draw a dragon", False, True),
    ("generate an image of a forest", False, True),
    # In a real test: a question was asked about an attached image and the model started a "regeneration".
    ("What do you see in this image? Describe it briefly.", True, False),
    ("What are the people in the image doing?", True, False),
    ("Read the text in this image.", True, False),
    ("Make it darker", True, True),
    ("Turn it into a night version", True, True),
    ("Change the background", True, True),
    ("Make it darker", False, False),
]


def test_generate_image_is_rejected_when_user_did_not_ask_for_an_image(tmp_path):
    """In a real UI test the model generated an image on its own for a weather question."""
    inference = read_text("agent/mamidlabs-inference.ps1")
    assert "!(Test-ImageRequestIntent $userText ([bool]$JobData.hasSourceImage -or (Test-ConversationHasImage $messages)))" in inference

    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh not found")
    functions = extract_function(inference, "Test-ImageRequestIntent") + "\n" + extract_function(inference, "Test-ConversationHasImage")
    cases = "\n".join(
        "Write-Output ([string](Test-ImageRequestIntent '{}' ${}))".format(text.replace("'", "''"), str(ctx).lower())
        for text, ctx, _ in IMAGE_REQUEST_CASES
    )
    history = """
$h = [Collections.ArrayList]::new(); [void]$h.Add(@{ role = 'user'; content = 'hello' })
Write-Output ([string](Test-ConversationHasImage $h))
[void]$h.Add(@{ role = 'assistant'; content = '[Image generated: 1024x1024, seed 1, mode new, file: x.png] Prompt: cat' })
Write-Output ([string](Test-ConversationHasImage $h))
$h2 = [Collections.ArrayList]::new(); [void]$h2.Add(@{ role = 'user'; content = @(@{ type = 'text'; text = 'look' }, @{ type = 'image_url'; image_url = @{ url = 'data:' } }) })
Write-Output ([string](Test-ConversationHasImage $h2))
"""
    script = tmp_path / "image_intent.ps1"
    script.write_text(functions + "\n" + cases + "\n" + history, encoding="utf-8-sig")
    result = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-File", str(script)],
        capture_output=True, encoding="utf-8", errors="replace", check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    got = [line.strip() == "True" for line in result.stdout.splitlines() if line.strip()]
    expected = [e for _, _, e in IMAGE_REQUEST_CASES] + [False, True, True]
    assert got == expected, list(zip([t for t, _, _ in IMAGE_REQUEST_CASES] + ["history:none", "history:generated", "history:attached"], got))


def test_turn_hint_is_not_mistaken_for_user_text_in_image_messages(tmp_path):
    """In a message with an image the desktop's '[System note]' part was taken for user text,
    so the note's 'reading text' phrase sent every image question to OCR."""
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh not found")
    inference = read_text("agent/mamidlabs-inference.ps1")
    functions = "\n".join(extract_function(inference, n) for n in ("Get-PlainText", "Get-CurrentUserText", "Test-OcrIntent", "Test-ImageRequestIntent"))
    script = tmp_path / "hint.ps1"
    script.write_text(functions + """
$note = '[System note] A new image was attached in this turn. For requests to read text/writing use the OCR result. Do not call generate_image unless the user explicitly asks for generation.'
$m = [Collections.ArrayList]::new()
[void]$m.Add(@{ role = 'user'; content = @(@{ type = 'text'; text = $note }, @{ type = 'text'; text = 'What do you see in this image?' }, @{ type = 'image_url'; image_url = @{ url = 'data:' } }) })
$t = Get-CurrentUserText $m
Write-Output $t
Write-Output ([string](Test-OcrIntent $t))
Write-Output ([string](Test-ImageRequestIntent $t $true))
$s = [Collections.ArrayList]::new(); [void]$s.Add(@{ role = 'user'; content = ($note + "`n`nhello") })
Write-Output (Get-CurrentUserText $s)
""", encoding="utf-8-sig")
    result = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-File", str(script)],
        capture_output=True, encoding="utf-8", errors="replace", check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    assert lines == ["What do you see in this image? [image]", "False", "False", "hello"], lines


def test_realism_lora_prefix_is_rewritten_without_touching_tensor_data(tmp_path):
    """fal's krea-2 training tool writes the keys with the base_model.model. prefix;
    ComfyUI expects diffusion_model. and silently skips the ones that do not match."""
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh not found")

    lora = tmp_path / "krea2_realism_lora.safetensors"
    tensors = {
        "base_model.model.blocks.0.attn.wq.lora_A.weight": struct.pack("<4f", 1, 2, 3, 4),
        "base_model.model.txtmlp.1.lora_B.weight": struct.pack("<2f", 5, 6),
    }
    metadata = {"base_model": "fal/krea-2", "lora_rank": "32"}
    original_data = write_safetensors(lora, tensors, metadata)

    function = extract_function(read_text("agent/imagegen-run.ps1"), "Repair-LoraKeyPrefix")
    script = tmp_path / "run.ps1"
    script.write_text(
        "$ErrorActionPreference = 'Stop'\n" + function +
        f"\nRepair-LoraKeyPrefix '{lora}'\nRepair-LoraKeyPrefix '{lora}'\n",
        encoding="utf-8-sig",
    )
    result = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-File", str(script)],
        capture_output=True, encoding="utf-8", errors="replace", check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout

    length, header, data = read_safetensors(lora)
    assert length % 8 == 0
    assert header.pop("__metadata__") == metadata
    assert set(header) == {
        "diffusion_model.blocks.0.attn.wq.lora_A.weight",
        "diffusion_model.txtmlp.1.lora_B.weight",
    }
    assert data == original_data
    assert not (tmp_path / "krea2_realism_lora.safetensors.tmp").exists()
