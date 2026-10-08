"""Contracts of the 8 GB VRAM / 16 GB RAM Lite profile and the profile catalog."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8-sig")


def parse_alias_table(text: str, variable: str) -> dict:
    """Turns a table of the form @{ max = 'a'; flash = 'b'; lite = 'c' } into a dict."""
    match = re.search(re.escape(variable) + r"\s*=\s*@\{([^}]*)\}", text)
    assert match, f"{variable} table not found"
    return dict(re.findall(r"(\w+)\s*=\s*'([^']+)'", match.group(1)))


# --------------------------------------------------------------- profile definition

def test_manager_accepts_lite_profile():
    manager = read_text("scripts/manage-qwen.ps1")

    assert "ValidateSet('max', 'flash', 'lite')" in manager
    # Lite uses a separate 9B model; it must not fall back to the 27B file.
    assert "Huihui-Qwen3.5-9B-abliterated.i1-Q5_K_M.gguf" in manager
    assert "$desiredAlias = $profileAliases[$Profile]" in manager


def test_alias_tables_agree_between_manager_and_registry():
    """If the alias tables in the two files diverge, the model restore returns to the wrong profile."""
    manager = parse_alias_table(read_text("scripts/manage-qwen.ps1"), "$profileAliases")
    registry = parse_alias_table(read_text("agent/model-registry.ps1"), "$script:localProfileAliases")

    assert manager == registry
    assert set(manager) == {"max", "flash", "lite"}
    assert len(set(manager.values())) == 3


def test_alias_table_is_reachable_when_only_functions_are_loaded():
    """If the table stayed at file level, calls that load only the functions
    (behavior tests, partial dot-source) would call a method on null."""
    registry = read_text("agent/model-registry.ps1")

    assert "function Get-LocalProfileAliases" in registry
    assert registry.index("function Get-LocalProfileAliases") < registry.index("$script:localProfileAliases = @{")
    # No direct access to the table may remain; everything must go through the accessor.
    assert "$script:localProfileAliases[" not in registry
    assert "$script:localProfileAliases.ContainsKey" not in registry


def test_lite_stays_within_eight_gigabyte_budget():
    """32K context was measured on real hardware (RTX 4060 Laptop). With Q4_K_M VRAM
    use is 6198 MiB (1759 MiB stays free, a comfortable margin). The user preferred to make
    Q5_K_M the default: 7500 MiB (~450-500 MiB stays free, a tight but accepted
    margin - llama.cpp's own automatic fit check marks this as "does not fully fit",
    but since --gpu-layers 99 is forced manually it loads anyway; performance does not
    drop (~35 tok/s, only ~13% slower than Q4))."""
    manager = read_text("scripts/manage-qwen.ps1")
    lite_block = manager.split("if ($Profile -eq 'lite') {", 1)[1]

    assert "$liteFile = 'Huihui-Qwen3.5-9B-abliterated.i1-Q5_K_M.gguf'" in lite_block
    assert "$liteCtx = '32768'" in lite_block
    assert "'--ctx-size', $liteCtx" in manager
    assert "'--batch-size', '256', '--ubatch-size', '128'" in manager
    # Without a projector Lite must run text-only, not fail.
    assert "$Profile -ne 'lite' -and !(Test-Path -LiteralPath $projector" in manager
    assert "if ($projector) { $serverArgs += @('--mmproj', $projector" in manager


def test_settings_are_loaded_before_the_profile_reads_them():
    """If $settings is assigned after the profile block, liteModel/liteContext are silently ignored."""
    manager = read_text("scripts/manage-qwen.ps1")

    assigned = manager.index("$settings = Get-Content -LiteralPath $settingsPath")
    used = manager.index("$settings.liteModel")
    assert assigned < used, "the settings are read after they are used"


def test_registry_exposes_lite_entry_matching_server_context():
    """32K context was measured on a real machine (see test_lite_stays_within_eight_gigabyte_budget);
    the UI's contextLimit (which sets the context compaction threshold) must match the server's
    real --ctx-size, otherwise compaction triggers earlier/later than needed."""
    registry = read_text("agent/model-registry.ps1")

    assert "id = 'builtin-lite'; label = 'Qwen3.5 9B LITE'; profile = 'lite'" in registry
    assert "contextLimit = 32768" in registry
    assert "function Get-ProfileFromAlias" in registry


def test_model_bar_only_lists_profiles_with_chat_files_present():
    """LITE could appear selectable on the bottom bar on a PC where only MAX was installed
    (or vice versa); selecting it made manage-qwen.ps1 fail with a missing-file error. Now
    only a profile whose language model files are on disk is listed."""
    registry = read_text("agent/model-registry.ps1")
    catalog = read_text("agent/profile-catalog.ps1")

    assert "function Get-BuiltinProfileDefinitions" in registry
    assert "if (Test-LocalProfileChatFilesPresent $def.profile)" in registry
    assert "function Test-LocalProfileChatFilesPresent" in catalog
    # ComfyUI media models are not needed for chat, they must stay out of the check.
    assert "-notmatch '^ComfyUI" in catalog


def test_chat_files_present_ignores_missing_comfyui_media_but_requires_llm_files(tmp_path):
    """LITE should not appear on a PC where only MAX is installed (or vice versa). A profile
    must be marked "not ready" if its language model files are missing; it must still be
    marked "ready" for chat even if ComfyUI media files are missing (may have used
    -SkipMedia)."""
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")

    (tmp_path / "models" / "max").mkdir(parents=True)
    (tmp_path / "models" / "max" / "model.gguf").write_bytes(b"x")
    (tmp_path / "models" / "max" / "mmproj.gguf").write_bytes(b"x")
    catalog_json = {
        "profiles": [
            {"id": "max", "files": [
                {"target": "models/max/model.gguf"},
                {"target": "models/max/mmproj.gguf"},
                {"target": "ComfyUI/models/diffusion_models/missing.safetensors"},
            ]},
            {"id": "lite", "files": [
                {"target": "models/lite/model.gguf"},
                {"target": "models/lite/mmproj.gguf"},
            ]},
        ]
    }
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "profiles.json").write_text(json.dumps(catalog_json), encoding="utf-8")

    catalog_path = str(ROOT / "agent" / "profile-catalog.ps1").replace("\\", "\\\\")
    root_arg = str(tmp_path).replace("\\", "\\\\")
    script = rf"""
$ErrorActionPreference = 'Stop'
function Get-Functions([string]$path) {{
    $tokens=$null; $errs=$null
    $ast=[Management.Automation.Language.Parser]::ParseFile($path,[ref]$tokens,[ref]$errs)
    if ($errs.Count) {{ throw ($errs | Out-String) }}
    return (($ast.EndBlock.Statements | Where-Object {{ $_ -is [Management.Automation.Language.FunctionDefinitionAst] }} | ForEach-Object {{ $_.Extent.Text }}) -join "`n")
}}
$root = '{root_arg}'
Invoke-Expression (Get-Functions '{catalog_path}')
Write-Output ('max:' + (Test-LocalProfileChatFilesPresent 'max'))
Write-Output ('lite:' + (Test-LocalProfileChatFilesPresent 'lite'))
"""
    proc = subprocess.run([pwsh, "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert "max:True" in proc.stdout, proc.stdout
    assert "lite:False" in proc.stdout, proc.stdout


def test_desktop_resolves_profile_through_alias_helper():
    desktop = read_text("qwen-desktop.ps1")

    # The old "-like '*-flash'" derivation existed in three places; it took lite for max.
    assert "-like '*-flash'" not in desktop
    assert desktop.count("Get-ProfileFromAlias $script:activeModel") == 3
    assert "'builtin-lite' { 'lite'; break }" in desktop


def test_lite_bounds_thinking_with_a_server_enforced_budget():
    """The Qwen3.5 template ignores --reasoning-effort, so effort can only be tuned
    with a server-enforced token budget.

    Measured on an RTX 4060, the same request:
      unlimited   -> 1451-2391 tokens, ~50 s
      budget 300  ->  408-429 tokens, ~10 s (answer quality preserved)
      budget 0    ->  175 tokens, ~4 s (thinking fully off)
    """
    manager = read_text("scripts/manage-qwen.ps1")

    assert "$liteThinkBudget = '300'" in manager
    assert "'--reasoning-budget', $liteThinkBudget," in manager
    # It must be changeable from the settings.
    assert "$settings.liteReasoningBudget" in manager


def test_thinking_is_never_switched_off_in_requests():
    """Thinking is a product decision: the budget limits its length, it does not turn the feature off."""
    inference = read_text("agent/qwen-inference.ps1")

    assert "enable_thinking" not in inference
    assert "function Set-LowEffortRequest" in inference
    # Definition + context compaction + main request + empty-answer retry.
    assert inference.count("Set-LowEffortRequest") == 4
    assert "$body.reasoning_effort = 'low'" in inference


def test_lite_system_prompt_encourages_deep_reasoning():
    """The LITE system prompt gets a deep, step-by-step reasoning instruction; the line
    is only present for the lite profile (the prefix is fixed per profile, so the KV
    cache is not broken) and steers toward a more accurate result within the thinking
    budget."""
    desktop = read_text("qwen-desktop.ps1")

    assert "Think deeply and carefully" in desktop
    idx = desktop.index("Think deeply and carefully")
    block = desktop[max(0, idx - 700):idx]
    assert "$script:apiSettings.localProfile -eq 'lite'" in block
    # It does not switch thinking off; it only steers it.
    assert "enable_thinking" not in desktop


# ------------------------------------------------------------------ media path

def test_lite_image_uses_krea2_gguf_via_shared_workflow():
    """At the user's request: Z-Image quality was found insufficient, Lite was changed to
    use the same Krea-2 Turbo as MAX/FLASH - only instead of the full-precision
    version (.safetensors, ~13.1 GB, does not fit an 8 GB card) the GGUF quantized
    (Q4_K_M, ~7.5 GB) version and the 'UnetLoaderGGUF' node of the ComfyUI-GGUF custom
    node are used. There is no separate New-LiteWorkflow anymore - the single
    New-Workflow function branches on the $lite parameter (to avoid code duplication,
    in the same spirit as the media-model-swap.ps1 pattern from #17)."""
    imagegen = read_text("agent/imagegen-run.ps1")

    assert "function New-LiteWorkflow" not in imagegen
    assert "class_type = 'UnetLoaderGGUF'" in imagegen
    assert "krea2_turbo-Q4_K_M.gguf" in imagegen
    assert "$workflow = New-Workflow $mode $JobData.prompt $width $height $seed $steps $refName $strength $realism $realismStrength $lite" in imagegen
    # The GGUF files live under models\unet, where the ComfyUI-GGUF custom node looks.
    assert "$modelFolder = if ($lite) { 'models\\unet' }" in imagegen
    assert "Start-OrReuseComfy $python $comfyDir $comfyOut $comfyIn $logs $comfyUrl $lite" in imagegen


def test_lite_image_supports_style_reference_and_realism_like_max_flash():
    """At the user's request: the realism/style reference LoRAs now work on Lite the same
    way as on MAX/FLASH - no silent fallback to another mode and no disabling
    (these LoRAs did not exist at all in the Z-Image AIO architecture, now all of them
    share the same Krea-2 architecture)."""
    imagegen = read_text("agent/imagegen-run.ps1")

    assert "if ($lite -and $mode -eq 'style_reference')" not in imagegen
    assert "if ($lite) { $realism = $false }" not in imagegen
    # If the style reference LoRA file is missing (lite included) an explicit error must be given.
    assert "if ($mode -eq 'style_reference' -and !(Test-Path -LiteralPath $styleLora -PathType Leaf))" in imagegen


def test_lite_video_caps_resolution_and_duration():
    videogen = read_text("agent/videogen-run.ps1")

    assert "function Limit-LiteResolution" in videogen
    assert "$maxSeconds = if ($lite) { 3 } else { 5 }" in videogen
    assert "$capped = Limit-LiteResolution $width $height" in videogen
    assert "Start-OrReuseComfy $python $comfyDir $comfyOut $comfyIn $logs $comfyUrl $lite" in videogen
    # The Comfy-Org repository has no fp8 of ti2v_5B; Lite also uses the fp16 file.
    assert "wan2.2_ti2v_5B_fp8_scaled" not in videogen
    assert "tile_size = $tile.size" in videogen


# ------------------------------------------------------------- profile catalog

def load_catalog() -> dict:
    return json.loads(read_text("agent/profiles.json"))


def test_catalog_lists_every_profile_the_manager_accepts():
    catalog = load_catalog()
    ids = [profile["id"] for profile in catalog["profiles"]]

    assert ids == ["max", "flash", "lite"]
    for profile in catalog["profiles"]:
        assert profile["label"] and profile["hardware"] and profile["summary"]
        assert profile["files"], f"{profile['id']} has no files"


@pytest.mark.parametrize("field", ["label", "target"])
def test_catalog_files_carry_required_fields(field):
    for profile in load_catalog()["profiles"]:
        for entry in profile["files"]:
            assert entry.get(field), f"{profile['id']}: {field} empty"


def test_catalog_targets_stay_inside_the_install_root():
    for profile in load_catalog()["profiles"]:
        for entry in profile["files"]:
            target = entry["target"]
            assert not target.startswith("/"), target
            assert ":" not in target, target
            assert ".." not in target.split("/"), target
            assert "\\" not in target, target


def test_catalog_download_entries_are_complete():
    """Every row with a repo must be downloadable: a file path and a size are required."""
    for profile in load_catalog()["profiles"]:
        for entry in profile["files"]:
            if not entry["repo"]:
                # Rows with no source defined are installed manually; no size is expected.
                assert not entry["filename"]
                continue
            assert "/" in entry["repo"], entry["repo"]
            assert entry["filename"], entry["target"]
            assert entry["bytes"] > 0, entry["target"]


def test_every_profile_is_fully_downloadable():
    """Choosing a profile in the wizard and pressing Next must download everything: no row,
    including MAX/FLASH's 27B language model and the Identity Edit LoRA, needs a manual install."""
    for profile in load_catalog()["profiles"]:
        manual = [e["target"] for e in profile["files"] if not e["repo"]]
        assert not manual, f"{profile['id']} needs a manual install: {manual}"

    lite = next(p for p in load_catalog()["profiles"] if p["id"] == "lite")
    names = [entry["target"].rsplit("/", 1)[-1] for entry in lite["files"]]
    assert "Huihui-Qwen3.5-9B-abliterated.i1-Q5_K_M.gguf" in names
    assert "krea2_turbo-Q4_K_M.gguf" in names
    assert "qwen3vl_4b_fp8_scaled.safetensors" in names
    assert "qwen_image_vae.safetensors" in names


def test_lite_language_model_matches_manager_default():
    """The GGUF in the catalog and the file manage-qwen.ps1 opens must be the same."""
    lite = next(p for p in load_catalog()["profiles"] if p["id"] == "lite")
    gguf = [
        e
        for e in lite["files"]
        if e["target"].endswith(".gguf")
        and e["target"].startswith("models/Huihui-Qwen3.5-9B-abliterated-i1-GGUF/")
        and "mmproj" not in e["target"]
    ]

    assert len(gguf) == 1
    name = gguf[0]["target"].rsplit("/", 1)[-1]
    assert name in read_text("scripts/manage-qwen.ps1")
    # manage-qwen.ps1 looks for the file in this folder.
    assert gguf[0]["target"].startswith("models/Huihui-Qwen3.5-9B-abliterated-i1-GGUF/")


def test_pinned_revision_is_used_for_repackaged_wan_files():
    """A download without a pinned version silently fetches another file when the repository is updated."""
    for profile in load_catalog()["profiles"]:
        for entry in profile["files"]:
            if entry["repo"] in (
                "Comfy-Org/Wan_2.2_ComfyUI_Repackaged",
                "Comfy-Org/Krea-2",
                "vantagewithai/Krea-2-Turbo-GGUF",
            ):
                assert re.fullmatch(r"[0-9a-f]{40}", entry.get("revision", "")), entry["target"]


# -------------------------------------------------------------- download flow

def test_downloader_supports_multiple_files_without_token():
    hub = read_text("agent/hf-hub.py")

    assert "files = job.get('files')" in hub
    assert "def fetch(" in hub
    # The catalog repositories are public; a token must not be mandatory.
    assert "raise ValueError('Save your Hugging Face token first.')" not in hub
    assert "only public repositories" in hub
    # An existing file must not be downloaded again.
    assert "if target.exists() and not item.get('force')" in hub


def test_downloader_rejects_paths_outside_the_repository():
    hub = read_text("agent/hf-hub.py")

    assert "def safe_repo_path" in hub
    assert "'..' in parts" in hub
    assert "safe_repo_path(item['filename'])" in hub


def test_profile_menu_is_wired_into_the_desktop():
    desktop = read_text("qwen-desktop.ps1")
    catalog = read_text("agent/profile-catalog.ps1")

    assert "profile-catalog.ps1')" in desktop
    assert "ToolStripMenuItem('Profiles')" in desktop
    # Menu order: File leftmost, View in the middle, Profiles rightmost.
    assert desktop.index("$menu.Items.Add($fileMenu)") < desktop.index("$menu.Items.Add($viewMenu)")
    assert desktop.index("$menu.Items.Add($viewMenu)") < desktop.index("$menu.Items.Add($profileMenu)")
    assert "$profileMenu.Add_DropDownOpening" in desktop

    for name in ("Get-ProfileCatalog", "Get-ProfileState", "Show-ProfileManager",
                 "Switch-ToProfile", "Start-ProfileDownload"):
        assert f"function {name}" in catalog


def test_profile_switch_blocks_while_busy():
    catalog = read_text("agent/profile-catalog.ps1")

    assert "if ($script:busy -or $script:imageBusy -or $script:pendingSend)" in catalog
    assert "if ($script:busy -or $script:imageBusy) { return 'Wait for the current operation to finish first.' }" in catalog


def test_imagegen_and_videogen_share_a_single_model_swap_module():
    """The stop/generate/restore flow has a single owner: imagegen-run.ps1 and
    videogen-run.ps1 both dot-source agent/media-model-swap.ps1 and use the same
    Stop-LocalModelForMedia / Restore-LocalModelForMedia functions - the old copied
    "& $manage -Action stop" wait loop must be gone."""
    for path in ("agent/imagegen-run.ps1", "agent/videogen-run.ps1"):
        text = read_text(path)
        assert ". (Join-Path $root 'agent\\media-model-swap.ps1')" in text
        assert "Stop-LocalModelForMedia $manage" in text
        assert "Restore-LocalModelForMedia $manage ([string]$JobData.restoreProfile) $false" in text
        assert "Restore-LocalModelForMedia $manage ([string]$JobData.restoreProfile) $true" in text
        # The old copied wait loop and restore blocks must be gone.
        assert "@(Get-Process -Name 'llama-server' -ErrorAction SilentlyContinue).Count -gt 0) {" not in text


def test_restore_status_is_visible_on_cancel_and_error_too():
    """Measured: after a cancel restoreProfile ran, but the 'Reloading the language model'
    status message of the success path was missing in the catch block; the user went through
    a silent wait without seeing why the model came back. Now it is implemented in one place
    (media-model-swap.ps1) and used on both call paths
    (success: $swallowError=$false, error: $swallowError=$true)."""
    swap = read_text("agent/media-model-swap.ps1")
    assert "function Restore-LocalModelForMedia" in swap
    assert 'Emit \'status\' "Reloading the language model ($restoreProfile)..."' in swap


def test_restore_failure_inside_catch_block_is_surfaced_not_swallowed():
    """If the restore after an error/cancel also fails (e.g. the model file was moved,
    the GPU is busy) it is not swallowed: media-model-swap.ps1 returns the error as text
    when $swallowError=$true, and the caller shows it together with the original
    image/video error."""
    swap = read_text("agent/media-model-swap.ps1")
    assert "if ($swallowError) {" in swap
    assert "catch { return $_.Exception.Message }" in swap

    for path in ("agent/imagegen-run.ps1", "agent/videogen-run.ps1"):
        text = read_text(path)
        assert "In addition, the language model could not be reloaded" in text
        # The old silent swallowing pattern ("try { ... } catch { }") must be gone.
        assert "-Profile ([string]$JobData.restoreProfile) | Out-Null } catch { }" not in text
