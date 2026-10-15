"""scripts/smoke-test.ps1 contracts.

After installation the smoke test verifies chat, document, OCR, image, photo editing,
post-media model switch and video with the real models. This file checks the script's
static contracts; the real run happens on an installed machine.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8-sig")


def test_smoke_skip_flags_include_ui():
    script = read_text("scripts/smoke-test.ps1")
    for flag in ("SkipChat", "SkipDocument", "SkipImage", "SkipVideo", "SkipUi"):
        assert f"[switch]${flag}" in script


def test_ui_step_launches_real_app_and_checks_mtp_and_no_connection_error():
    """UI step: launches the real app, verifies the window + model are ready, runs one turn
    with the real worker the UI uses, and proves there is no 'connection dropped' / watchdog
    text and that MTP works."""
    script = read_text("scripts/smoke-test.ps1")

    assert "Join-Path $Root 'mamidlabs-desktop.ps1'" in script
    assert "'-STA'" in script
    assert "agent\\mamidlabs-inference.ps1" in script
    assert "--spec-type\\s+draft-mtp" in script
    assert "draft_n_accepted" in script
    # Unwanted drop/close text is searched LITERALLY (.Contains); -like "*[Error]*" is a
    # character class in PowerShell and gives false positives.
    assert "$joined.Contains($marker)" in script
    assert '-like "*$marker*"' not in script
    for marker in ("forcibly closed", "connection dropped", "WSAECONNRESET", "[Error]"):
        assert marker in script
    assert "Report 'ui'" in script


def test_ui_step_restores_settings_and_stops_the_server():
    """The app overwrites the settings on exit and stops its own server; the step must back
    up and restore the settings and stop the server."""
    script = read_text("scripts/smoke-test.ps1")
    assert "mamidlabs-smoke-settings-backup" in script
    assert "Copy-Item -LiteralPath $settingsDir -Destination $settingsBackup" in script
    assert "Copy-Item -LiteralPath $settingsBackup -Destination $settingsDir" in script
    assert "& $manage -Action stop" in script


def test_smoke_test_has_ui_chat_step():
    """A step that drives the UI LIKE A USER: types into the prompt box, clicks Send and
    verifies a response arrived (Send->Stop->Send transition). The existing 'ui' step calls
    the worker directly; this step exercises the UI's real user path."""
    script = read_text("scripts/smoke-test.ps1")
    assert "Write-Section 'UI chat" in script
    assert "Report 'ui_chat'" in script
    assert "ValuePattern" in script  # types into the prompt box
    assert "0x00F5" in script        # BM_CLICK (Send)


def test_smoke_test_closes_leftover_comfyui():
    """The image/video steps leave ComfyUI open; the UI step force-kills the app so
    Shutdown-All does not run. At the end of the test (and in the UI step's finally) any
    idle ComfyUI is closed; otherwise the language model loads on top of it and spills VRAM
    (measured on the real machine: 0.89 tok/s)."""
    script = read_text("scripts/smoke-test.ps1")
    assert "& $manage -Action closecomfy" in script
    # Belt-and-suspenders: any leftover python ComfyUI process is killed too.
    assert "ComfyUI\\main.py" in script


def test_each_component_reports_pass_or_fail_independently():
    """If one component fails the others must still be attempted; there must be no early exit."""
    script = read_text("scripts/smoke-test.ps1")

    # Each block is inside its own try/catch; even with $ErrorActionPreference='Stop' at the
    # top level, one step's error must not block the others.
    assert script.count("} catch {") >= 4
    assert script.count("Report '") >= 4


def test_exit_code_reflects_overall_result():
    script = read_text("scripts/smoke-test.ps1")

    assert "exit 1" in script
    assert "exit 0" in script


def test_image_and_video_checks_require_the_model_file_first():
    """If the model file is missing it must give an explicit error without starting ComfyUI for nothing."""
    script = read_text("scripts/smoke-test.ps1")

    assert "Download it from the Profiles menu first" in script
    # On Lite it is looked up under unet (GGUF), on the other profiles under diffusion_models.
    assert "models\\unet" in script
    assert "models\\diffusion_models" in script


def test_document_check_uses_a_real_docx_round_trip():
    """Not a mock: a real file must be created with python-docx and read back with
    office-tools.py; the marker text must be searched for in the output."""
    script = read_text("scripts/smoke-test.ps1")

    assert "from docx import Document" in script
    assert "office-tools.py" in script
    assert "marker" in script.lower()


def test_ocr_check_covers_both_gpu_and_cpu_engines():
    """Measured on an RTX 4060 Laptop: the dev-branch RTX-50 wheel really works on the Ada
    architecture too (confidence 0.999), it does not just return 'ready:true' with an empty
    result. The plain image path for the GPU and the scanned PDF path for the CPU are tested
    separately; they are different code paths (ocr_image vs ocr_pdf)."""
    script = read_text("scripts/smoke-test.ps1")

    assert "[switch]$SkipOcr" in script
    assert "action = 'ocr_image'" in script
    assert "action = 'ocr_pdf'" in script
    assert r"venv\ocr\Scripts\python.exe" in script
    assert r"venv\ocr-cpu\Scripts\python.exe" in script


def test_readme_documents_the_smoke_test():
    readme = read_text("README.md")
    assert "smoke-test.ps1" in readme


def test_image_and_video_jobdata_pass_root():
    """The image/video jobData must carry the 'root' field. Since imagegen-run.ps1/videogen-run.ps1 run with
    Start-ThreadJob -FilePath, $PSScriptRoot is empty (see
    test_install_root_portability.py); without JobData.root it fell back to
    (Get-Location).Path and took the folder smoke-test.ps1 was RUN FROM (the source tree)
    as the root, giving the error "Image/video environment not found: ...source-tree\\venv\\imagegen\\..."
    - the -Root parameter was ignored completely."""
    script = read_text("scripts/smoke-test.ps1")
    image_block = script.split("Write-Section 'Image generation'")[1].split("Write-Section 'Video generation'")[0]
    video_block = script.split("Write-Section 'Video generation'")[1]
    assert "root = $Root" in image_block
    assert "root = $Root" in video_block


def test_chat_check_matches_production_request_shape():
    """Caught on a real machine: the chat test called the raw /v1/chat/completions
    without reasoning_effort='low' and with max_tokens=200. Lite's thinking is now
    unlimited (no budget); the model spent this small limit thinking and cut off before
    producing real content - the "The model returned an empty answer" error. The real
    generation request (Set-LowEffortRequest in
    mamidlabs-inference.ps1) always sends reasoning_effort; the smoke test must use the same
    shape."""
    script = read_text("scripts/smoke-test.ps1")
    # The request lives in Invoke-ChatProbe; the chat step and the post-media switch step use the same shape.
    assert "$probe = Invoke-ChatProbe" in script.split("Write-Section 'Chat model'")[1].split("Write-Section 'Document reading")[0]
    chat_block = script.split("function Invoke-ChatProbe")[1].split("\n}\n")[0]
    assert "reasoning_effort = 'low'" in chat_block
    assert "chat_template_kwargs = @{ reasoning_effort = 'low' }" in chat_block
    # 200 tokens can be spent entirely on (unlimited) thinking; a realistic margin must be left.
    assert "max_tokens = 200" not in chat_block


def test_document_check_uses_a_valid_office_tools_action():
    """Caught on a real machine: office-tools.py's ACTIONS table has no action named
    'read_document' (only read_docx/read_pdf/...; 'read_document' is only the tool NAME
    inside mamidlabs-inference.ps1, where it is mapped to the right action by extension).
    Since the smoke test calls office-tools.py directly it must use the real action
    name - if it is wrong it gives an "unknown action" error and the test itself always
    fails."""
    script = read_text("scripts/smoke-test.ps1")
    doc_block = script.split("Write-Section 'Document reading")[1].split("Write-Section 'OCR")[0]
    assert "action = 'read_docx'" in doc_block
    assert "action = 'read_document'" not in doc_block


def test_media_swap_check_measures_speed_after_image_generation():
    """Model switch after an image (8 GB LITE): the image step unloads and reloads the language
    model as in the real app, the next step compares the speed with the first measurement."""
    script = read_text("scripts/smoke-test.ps1")
    image_block = script.split("Write-Section 'Image generation'")[1].split("Write-Section 'Photo editing")[0]
    assert "restoreProfile = $(if ($script:chatBaseline) { $Profile } else { '' })" in image_block
    swap_block = script.split("Write-Section 'Post-media model switch'")[1].split("Write-Section 'Video generation'")[0]
    assert "Wait-ForHealth 180" in swap_block
    assert "$ratio -lt $MinSpeedRatio" in swap_block
    assert "Get-FreeVramMiB" in swap_block
    assert "[double]$MinSpeedRatio = 0.7" in script


def test_smoke_test_writes_a_machine_readable_report():
    script = read_text("scripts/smoke-test.ps1")
    assert "smoke-test-last.json" in script
    assert "ConvertTo-Json -Depth 6" in script
    # A skipped step does not count as a failure but shows up in the summary.
    assert "skipped = $true" in script


def test_history_resume_step_guards_empty_tool_exchange():
    """When no tool is called, $done.data is an EMPTY array that comes back as $null across the
    Start-ThreadJob boundary; @($done.data) then wraps it into @($null), adding a blank message
    and making the server return 400 ("All non-assistant messages must contain 'content'"). The
    step must guard with `if ($done.data)` (like the real UI) and require >=1 reloaded message."""
    script = read_text("scripts/smoke-test.ps1")
    assert "if ($done.data) { $chatMessages += @($done.data) }" in script
    assert "$reloaded.Count -lt 1" in script
