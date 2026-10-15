"""Krea 2 Identity Edit (photo editing) integration.

The real workflow functions in imagegen-run.ps1 are run and the resulting API-format
ComfyUI graph is checked against the INPUT_TYPES contract of the comfyui-krea2edit node
pack (pinned commit 86f886d) and the UI template in the repository.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# comfyui-krea2edit@86f886d __init__.py INPUT_TYPES (required + optional).
PATCH_INPUTS = {"model", "source_latent", "source_latent_b", "ref_boost", "ref_boost_a", "fit_mode",
                "ref_boost_mask", "vae", "source_image", "source_image_b", "target_latent"}
ENCODE_INPUTS = {"clip", "prompt", "image", "image_b", "grounding_px", "system_prompt"}


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8-sig")


def build_workflow(tmp_path, call: str) -> dict:
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")
    script = tmp_path / "wf.ps1"
    script.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        f"$source = [IO.File]::ReadAllText('{ROOT / 'agent' / 'imagegen-run.ps1'}')\n"
        "$tokens = $null; $errors = $null\n"
        "$ast = [Management.Automation.Language.Parser]::ParseInput($source, [ref]$tokens, [ref]$errors)\n"
        "$functions = $ast.FindAll({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] }, $false)\n"
        ". ([scriptblock]::Create((($functions | ForEach-Object { $_.Extent.Text }) -join \"`n\")))\n"
        "$identityLoraName = 'krea2_identity_edit_v1_2.safetensors'\n"
        "$JobData = @{}\n"
        f"{call} | ConvertTo-Json -Depth 20 -Compress\n",
        encoding="utf-8-sig",
    )
    result = subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-File", str(script)],
                            capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def nodes_of(workflow: dict, class_type: str) -> list:
    return [node for node in workflow.values() if node["class_type"] == class_type]


def assert_links_resolve(workflow: dict):
    for node_id, node in workflow.items():
        for name, value in node["inputs"].items():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[1], int):
                assert value[0] in workflow, f"{node_id}.{name} -> {value[0]} missing"


def test_single_image_edit_matches_node_contract(tmp_path):
    wf = build_workflow(tmp_path, "New-Workflow 'identity_edit' 'Change her outfit to a red raincoat.' 1024 1024 42 10 'ref.png' 0.6 $true 0.8 $false")
    assert_links_resolve(wf)

    patch = nodes_of(wf, "Krea2EditModelPatch")
    assert len(patch) == 1
    assert set(patch[0]["inputs"]) <= PATCH_INPUTS
    assert patch[0]["inputs"]["ref_boost"] == 4.0
    assert patch[0]["inputs"]["fit_mode"] == "fit"
    # Pixel path + target_latent: the source is encoded before sampling starts.
    assert patch[0]["inputs"]["target_latent"] == ["82", 0]
    assert "source_latent_b" not in patch[0]["inputs"]

    encodes = nodes_of(wf, "Krea2EditGroundedEncode")
    assert len(encodes) == 2
    for node in encodes:
        assert set(node["inputs"]) <= ENCODE_INPUTS
        assert node["inputs"]["grounding_px"] == 768
    prompts = sorted(node["inputs"]["prompt"] for node in encodes)
    assert prompts == ["", "Change her outfit to a red raincoat."]

    lora = nodes_of(wf, "LoraLoaderModelOnly")
    assert [n["inputs"]["lora_name"] for n in lora] == ["krea2_identity_edit_v1_2.safetensors"]
    # The realism LoRA does not enter the editing chain.
    assert "70" not in wf

    sampler = wf["3"]["inputs"]
    assert sampler["model"] == ["79", 0]
    assert sampler["latent_image"] == ["82", 0]
    assert (sampler["steps"], sampler["cfg"], sampler["sampler_name"], sampler["scheduler"]) == (10, 1, "euler", "simple")
    assert wf["82"]["class_type"] == "EmptySD3LatentImage"
    assert wf["10"]["class_type"] == "UNETLoader"


def test_two_image_edit_uses_scene_first_and_person_second(tmp_path):
    wf = build_workflow(tmp_path, "New-Workflow 'identity_edit' 'Place this person at the cafe table.' 1024 768 1 10 'scene.png' 0.6 $false 0.8 $false 'person.png'")
    assert_links_resolve(wf)
    patch = nodes_of(wf, "Krea2EditModelPatch")[0]["inputs"]
    assert wf[patch["source_image"][0]]["inputs"]["image"] == "scene.png"
    assert wf[patch["source_image_b"][0]]["inputs"]["image"] == "person.png"
    assert patch["source_latent_b"] == ["92", 0]
    for node in nodes_of(wf, "Krea2EditGroundedEncode"):
        assert wf[node["inputs"]["image_b"][0]]["inputs"]["image"] == "person.png"


def test_lite_edit_keeps_the_gguf_loader(tmp_path):
    wf = build_workflow(tmp_path, "New-Workflow 'identity_edit' 'Relight with golden hour.' 1024 1024 1 10 'ref.png' 0.6 $false 0.8 $true")
    assert wf["10"]["class_type"] == "UnetLoaderGGUF"
    assert wf["71"]["inputs"]["model"] == ["10", 0]


def test_other_modes_are_unchanged(tmp_path):
    wf = build_workflow(tmp_path, "New-Workflow 'new' 'a cat' 1024 1024 1 8 $null 0.6 $false 0.8 $false")
    assert not nodes_of(wf, "Krea2EditModelPatch")
    assert wf["3"]["inputs"]["model"] == ["10", 0]


@pytest.mark.parametrize("source,expected", [
    ((1024, 1024), (1024, 1024)),
    ((4000, 3000), (1184, 880)),
    ((1080, 1920), (768, 1360)),
])
def test_edit_resolution_is_one_megapixel_and_multiple_of_16(tmp_path, source, expected):
    size = build_workflow(tmp_path, f"Get-EditResolution {source[0]} {source[1]}")
    assert tuple(size) == expected
    assert size[0] % 16 == 0 and size[1] % 16 == 0


def test_reference_workflow_ships_with_the_repo_and_matches_the_lora_path():
    template = json.loads(read_text("agent/workflows/krea2_identity_edit.json"))
    types = {node["type"] for node in template["nodes"]}
    assert {"Krea2EditModelPatch", "Krea2EditGroundedEncode", "LoraLoaderModelOnly"} <= types
    lora = [n for n in template["nodes"] if n["type"] == "LoraLoaderModelOnly"][0]
    assert lora["widgets_values"][0] == "krea2_identity_edit_v1_2.safetensors"


def test_missing_node_pack_or_lora_fails_with_actionable_message():
    imagegen = read_text("agent/imagegen-run.ps1")
    assert "custom_nodes\\comfyui-krea2edit\\__init__.py" in imagegen
    assert "install-mamidlabs.ps1 -Stage comfyui" in imagegen
    assert "Download missing" in imagegen


def test_installer_pins_the_node_pack():
    installer = read_text("scripts/install-mamidlabs.ps1")
    assert "$script:Krea2EditCommit = '86f886dac23013d88996e3a2e99093ba44d322fb'" in installer
    assert "Install-PinnedCustomNode $target 'comfyui-krea2edit'" in installer


def test_catalog_lists_the_identity_lora_for_max_and_flash_but_not_lite():
    """identity_edit is not offered on LITE (extremely slow on 8 GB VRAM, measured 641-884 s -
    see the hardware table in AGENTS.md); downloading the LoRA would be a needless ~1.7 GB."""
    profiles = json.loads(read_text("agent/profiles.json"))["profiles"]
    for profile in profiles:
        targets = [f["target"] for f in profile["files"]]
        has_lora = "ComfyUI/models/loras/krea2_identity_edit_v1_2.safetensors" in targets
        if profile["id"] == "lite":
            assert not has_lora, profile["id"]
        else:
            assert has_lora, profile["id"]


def test_lite_profile_does_not_offer_identity_edit_mode():
    inference = read_text("agent/mamidlabs-inference.ps1")
    assert "identityEditAvailable = -not [bool]$job.imageProfileLite" in inference
    imagegen = read_text("agent/imagegen-run.ps1")
    assert "if ($lite) { throw 'Photo editing" in imagegen
    desktop = read_text("mamidlabs-desktop.ps1")
    assert "imageProfileLite = ([string]$script:apiSettings.localProfile -eq 'lite')" in desktop


def test_edit_mode_is_offered_regardless_of_chat_provider():
    """The image pipeline is independent of the chat provider (the chat may be Claude, the image local);
    with a cloud image provider a request with mode != new also goes to the editing endpoint."""
    inference = read_text("agent/mamidlabs-inference.ps1")
    assert "@('new', 'regenerate', 'style_reference', 'identity_edit')" in inference
    api_media = read_text("agent/api-media.py")
    assert "job.get('mode') != 'new'" in api_media


def test_desktop_passes_scene_and_person_for_two_image_edits():
    desktop = read_text("mamidlabs-desktop.ps1")
    assert "function Set-LastUserImage" in desktop
    assert "sourceImageB = $sourceImageB" in desktop
    assert "$sourceImage = $script:previousUserImage" in desktop
