import subprocess
import shutil
import tempfile
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8-sig")


def test_custom_provider_base_url_is_passed_to_the_worker():
    """The 'custom' base URL only lives in the settings (apiSettings); the worker
    (Start-ThreadJob) does not read settings and $PSScriptRoot is empty. So the desktop
    must pass apiSettings + baseUrl into jobData and the worker must use them for real
    requests; otherwise the custom provider fails with "enter a base address" in chat
    (only verification works). Found in a real end-to-end test."""
    desktop = read_text("qwen-desktop.ps1")
    worker = read_text("agent/qwen-inference.ps1")
    assert desktop.count("apiSettings = $script:apiSettings") >= 2
    assert desktop.count("baseUrl = (Get-CustomBaseUrl)") >= 2
    assert "$script:apiSettings = if ($JobData.apiSettings)" in worker
    assert "$JobData.baseUrl" in worker
    idx_apply = worker.index("$script:apiSettings.providers['custom'] = @{ baseUrl")
    idx_endpoint = worker.index("$endpoint = Get-ProviderEndpoint $provider")
    assert idx_apply < idx_endpoint


def test_model_profiles_use_max_flash_and_lite_names():
    registry = read_text("agent/model-registry.ps1")
    manager = read_text("scripts/manage-qwen.ps1")

    assert "builtin-max" in registry
    assert "builtin-flash" in registry
    assert "builtin-xhigh" not in registry
    assert "builtin-low" not in registry
    assert "builtin-lite" in registry
    assert "ValidateSet('max', 'flash', 'lite')" in manager
    assert "--reasoning-effort', $reasoningEffort" in manager


def test_ocr_only_returns_text_without_explanation_request():
    inference = read_text("agent/qwen-inference.ps1")

    assert "function Test-OcrExplainIntent" in inference
    assert "$ocrDirect = !(Test-OcrExplainIntent $userText)" in inference
    assert "if ($ocrDirect)" in inference
    assert "Emit 'done' ([string]$ocrResult.text)" in inference
    assert "Emit 'done' $directPdfText" in inference


def test_image_ocr_swaps_local_model_and_command_tool_uses_approval():
    inference = read_text("agent/qwen-inference.ps1")
    ocr = read_text("agent/ocr-tools.py")
    desktop = read_text("qwen-desktop.ps1")

    assert "function Invoke-ImageOcrWithModelSwap" in inference
    assert "action = 'ocr_image'; path = [string]$paths[0]; device = 'gpu'" in inference
    assert "& (Get-ManageQwenPath) -Action stop" in inference
    assert "Start-LocalModelRestore ([string]$JobData.profile)" in inference
    assert "function Request-Approval([string]$toolName" in inference
    assert "Request-Approval 'run_command'" in inference
    assert "name = 'run_command'" in inference
    assert "profile = $profile" in desktop
    assert "engine=\"paddleocr-gpu\"" in ocr
    assert "engine = _new_engine(\"cpu\")" in ocr
    assert "GPU OCR found no text; OCR failed, sending the image to Qwen mmproj" in inference
    assert "action = 'ocr_images'; paths = @($paths); device = 'gpu'" in inference
    assert "name = 'read_images_ocr'" in inference
    assert "tool_choice = @{ type = 'function'; function = @{ name = 'read_images_ocr' } }" in inference
    assert "Multi-image reading on — you can upload several images." in inference
    assert "function Get-OcrCpuPythonExe { Join-Path (Get-InstallRoot) 'venv\\ocr-cpu\\Scripts\\python.exe' }" in inference
    assert "action = 'ocr_pdf'; path = $full; max_pages = 50 } (Get-OcrPyPath) (Get-OcrCpuPythonExe)" in inference
    assert "function Start-LocalModelRestore([string]$profile)" in inference
    assert "Start-Process -FilePath $powershell -ArgumentList $arguments" in inference
    assert "currentImagePaths = @($script:currentTurnImagePaths)" in desktop
    assert "multiImageOcr = [bool]$script:currentTurnMultiImageOcr" in desktop
    assert "$dlg.Multiselect = [bool]$script:multiImageOcrMode" in desktop
    assert "First send the message \"turn on multi-image reading mode\"" in desktop


def test_multi_image_selection_is_preserved_until_send_validation():
    desktop = read_text("qwen-desktop.ps1")

    add_image_start = desktop.index("function Add-SelectedImage")
    add_image_end = desktop.index("function Add-SelectedVideo", add_image_start)
    add_image = desktop[add_image_start:add_image_end]
    assert "$script:selectedImages.Clear()" not in add_image
    assert "if the explicit mode is on" in add_image


def test_provider_adapters_preserve_forced_tool_choice():
    """OpenAI-compatible providers take a forced tool choice as is. On Claude a forced choice
    returns 400 while thinking is on, so it is turned into 'auto' (test_api_providers)."""
    providers = read_text("agent/api-providers.ps1")

    assert "if ($body.tool_choice) { $request.tool_choice=$body.tool_choice }" in providers


def test_api_video_tools_do_not_advertise_unsupported_video_continuation():
    inference = read_text("agent/qwen-inference.ps1")

    assert "$videoModes = @('text', 'image')" in inference
    assert "$videoModes += 'video'" in inference
    assert "do not use mode=video." in inference


def test_stream_watchdog_uses_fresh_connection_and_survives_dropped_stream():
    """The local server can close a keep-alive connection that sits idle while a tool
    runs; reusing that stale connection failed mid-stream with "An existing connection
    was forcibly closed by the remote host." (WSAECONNRESET). The fix: local requests
    close the connection (a fresh one per turn) and a drop with answer text in hand is
    not treated as an error."""
    inference = read_text("agent/qwen-inference.ps1")

    assert "if ($provider -eq 'local') { $request.Headers.ConnectionClose = $true }" in inference
    assert "$result.finishReason = 'connection-closed'" in inference


def test_web_tool_failure_is_detected_and_discourages_workarounds():
    """When web search / page download fails because of the network, a system note
    stops the model from working around it with run_command (ping/curl...); the tool
    failure is detected reliably."""
    inference = read_text("agent/qwen-inference.ps1")
    desktop = read_text("qwen-desktop.ps1")

    assert "function Test-WebToolFailure" in inference
    assert "$script:webToolFailCount" in inference
    assert "Test-WebToolFailure $name $result" in inference
    # The note appended to the request copy (never written to persistent history); two-tier.
    assert "work around it" in inference
    assert "run_command (ping/curl" in inference
    assert "CLEARLY UNAVAILABLE" in inference
    # Hard guard against network-probe commands (a small model may ignore the note).
    assert "$script:webToolFailCount -gt 0 -and" in inference
    assert "No network command was run" in inference
    # After the 2nd web failure web tools are no longer offered (prevents a search loop).
    assert "$offeredTools = if ($script:webToolFailCount -ge 2)" in inference
    # If the user did not ask about files, the file tools are dropped too (no rummaging).
    assert "function Test-FileIntent" in inference
    assert "$blocked += @('list_bound_files', 'read_bound_file')" in inference
    assert "Test-FileIntent $userText" in inference
    # A preventive rule in the system prompt too.
    assert "do NOT try to work around it" in desktop


def test_run_command_is_only_offered_on_explicit_command_intent():
    """Unless the user explicitly asks for a command/terminal, run_command is NOT offered to
    the model; so the small model cannot divert to absurd command attempts on a web failure."""
    inference = read_text("agent/qwen-inference.ps1")

    assert "function Test-CommandIntent" in inference
    assert "if (Test-CommandIntent $userText) {" in inference
    idx = inference.index("name = 'run_command'")
    block = inference[idx - 400:idx]
    assert "Test-CommandIntent $userText" in block


def test_command_intent_detection_matrix():
    """Test-CommandIntent: true only on an explicit command/terminal intent."""
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")
    script = r"""
$job = @{ messages = @(@{ role = 'user'; content = 'x' }); compactOnly = $true; provider = 'local'; model = 'test'; contextLimit = 32768; maxTokens = 100; agent = $false; internet = $false; office = $false; documentCreate = $false }
$shared = @{ Cancel = $false }
. '__ROOT__\agent\qwen-inference.ps1' $job $shared | Out-Null
$cases = @(
  @{ t = 'run this command: ipconfig'; e = $true }
  @{ t = 'open the terminal and type dir'; e = $true }
  @{ t = 'run a powershell script'; e = $true }
  @{ t = 'ping google.com'; e = $true }
  @{ t = 'what is the einstein riddle'; e = $false }
  @{ t = "what's the weather in istanbul"; e = $false }
  @{ t = 'summarize that in one sentence'; e = $false }
  @{ t = 'edit the photo'; e = $false }
)
$fail = 0
foreach ($c in $cases) {
  $got = [bool](Test-CommandIntent $c.t)
  if ($got -ne [bool]$c.e) { $fail++; Write-Output ("FAIL: '{0}' got={1} expected={2}" -f $c.t, $got, $c.e) }
}
Write-Output ("FAILCOUNT=" + $fail)
""".replace("__ROOT__", str(ROOT))
    with tempfile.NamedTemporaryFile(mode="w", suffix=".ps1", encoding="utf-8-sig", delete=False) as handle:
        handle.write(script)
        script_path = Path(handle.name)
    try:
        result = subprocess.run(
            [str(pwsh), "-NoProfile", "-NonInteractive", "-File", str(script_path)],
            capture_output=True, encoding="utf-8", errors="replace", check=False,
        )
    finally:
        script_path.unlink(missing_ok=True)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "FAILCOUNT=0" in result.stdout, result.stdout


def test_web_tool_failure_detection_matrix():
    """Test-WebToolFailure: true only for web tools and only on failure."""
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")
    script = r"""
$job = @{ messages = @(@{ role = 'user'; content = 'x' }); compactOnly = $true; provider = 'local'; model = 'test'; contextLimit = 32768; maxTokens = 100; agent = $false; internet = $false; office = $false; documentCreate = $false }
$shared = @{ Cancel = $false }
. '__ROOT__\agent\qwen-inference.ps1' $job $shared | Out-Null
$cases = @(
  @{ n = 'search_web'; r = '{"error":"Web search failed"}'; e = $true }
  @{ n = 'search_web'; r = '[{"title":"x","href":"u"}]'; e = $false }
  @{ n = 'fetch_page'; r = '{"error":"URL host could not be resolved."}'; e = $true }
  @{ n = 'find_reference_image'; r = '{"error":"image search failed"}'; e = $true }
  @{ n = 'list_bound_files'; r = '{"error":"unrelated"}'; e = $false }
  @{ n = 'search_web'; r = ''; e = $true }
)
$fail = 0
foreach ($c in $cases) {
  $got = [bool](Test-WebToolFailure $c.n $c.r)
  if ($got -ne [bool]$c.e) { $fail++; Write-Output ("FAIL: $($c.n) got=$got expected=$($c.e)") }
}
Write-Output ("FAILCOUNT=" + $fail)
""".replace("__ROOT__", str(ROOT))
    with tempfile.NamedTemporaryFile(mode="w", suffix=".ps1", encoding="utf-8-sig", delete=False) as handle:
        handle.write(script)
        script_path = Path(handle.name)
    try:
        result = subprocess.run(
            [str(pwsh), "-NoProfile", "-NonInteractive", "-File", str(script_path)],
            capture_output=True, encoding="utf-8", errors="replace", check=False,
        )
    finally:
        script_path.unlink(missing_ok=True)
    assert result.returncode == 0, result.stderr or result.stdout
    assert "FAILCOUNT=0" in result.stdout, result.stdout


def test_ocr_model_swap_keeps_json_response_separate_from_status_events():
    inference = read_text("agent/qwen-inference.ps1")
    start = inference.index("function Invoke-ImageOcrWithModelSwap")
    end = inference.index("function Invoke-Tool", start)
    ocr_swap = inference[start:end]

    status_lines = [line for line in ocr_swap.splitlines() if "Emit 'status'" in line]
    assert status_lines
    assert all("| Out-Null" in line for line in status_lines)


def test_security_contracts_cover_ssrf_archive_and_local_output_paths():
    tools = read_text("agent/qwen-agent-tools.py")
    office = read_text("agent/office-tools.py")
    media = read_text("agent/api-media.py")
    video_tools = read_text("agent/media-tools.py")
    ocr = read_text("agent/ocr-tools.py")
    imagegen = read_text("agent/imagegen-run.ps1")
    videogen = read_text("agent/videogen-run.ps1")

    assert "def _validate_remote_url" in tools
    assert "any(not address.is_global for address in addresses)" in tools
    assert "class _SafeRedirectHandler" in tools
    assert "_read_limited(response, MAX_PAGE_BYTES)" in tools
    assert "_read_limited(response, MAX_REFERENCE_BYTES)" in tools
    assert "request.get('out_dir')" not in tools
    assert "MAX_ARCHIVE_ENTRY_BYTES" in office
    assert "_read_archive_entry(stream" in office
    assert "zf.read(entry)" not in office
    assert "rf.read(entry)" not in office
    assert "decode_image_payload" in media
    assert "MAX_VIDEO_BYTES" in media
    assert "MAX_VIDEO_FILE_BYTES" in video_tools
    assert "candidate.relative_to(OUTPUT_ROOT)" in video_tools
    assert "MAX_RENDER_PIXELS" in ocr
    assert "Resolve-ComfyOutput" in imagegen
    assert "Resolve-ComfyOutput" in videogen
    assert "Remove-Item -LiteralPath $sourcePath" in imagegen
    assert "Remove-Item -LiteralPath $sourcePath" in videogen


def test_chat_history_remains_account_encrypted():
    desktop = read_text("qwen-desktop.ps1")

    assert "MAMIDLABS-CHAT-DPAPI-V1" in desktop
    assert "Protect-ChatText $json" in desktop
    assert "Read-ChatData $file.FullName" in desktop
    assert "$loadedChatId -match '^[0-9a-fA-F]{32}$'" in desktop


def test_single_instance_check_verifies_the_window_class():
    """Another window carrying the 'MamidLabs' title (e.g. Explorer's TabProxyWindow
    or a video/media window) made the app think it was 'already open' and prevented
    it from EVER starting (seen on a real machine; the UI smoke step reported
    'window closed before opening'). The single-instance check now accepts only a
    real WinForms window (class name WindowsForms10.*)."""
    desktop = read_text("qwen-desktop.ps1")
    assert "GetClassName" in desktop
    assert "WindowsForms10.*" in desktop
    idx_find = desktop.index("[QwenWindow]::FindWindow([NullString]::Value, $appTitle)")
    idx_class = desktop.index("WindowsForms10.*", idx_find)
    idx_return = desktop.index("return", idx_find)
    assert idx_find < idx_class < idx_return


def test_remote_url_validation_blocks_local_and_non_http_targets():
    path = ROOT / "agent" / "qwen-agent-tools.py"
    spec = importlib.util.spec_from_file_location("qwen_agent_tools", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    for url in ("http://127.0.0.1/", "http://169.254.169.254/", "file:///etc/passwd", "http://localhost/"):
        with pytest.raises(ValueError):
            module._validate_remote_url(url)


def test_archive_reader_stops_before_unbounded_memory_growth():
    path = ROOT / "agent" / "office-tools.py"
    spec = importlib.util.spec_from_file_location("office_tools", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    class OversizedStream:
        def read(self, size):
            return b"x" * size

    with pytest.raises(ValueError):
        module._read_archive_entry(OversizedStream(), module.MAX_ARCHIVE_ENTRY_BYTES)


def test_previous_image_payload_is_not_kept_for_follow_up_messages():
    desktop = read_text("qwen-desktop.ps1")
    inference = read_text("agent/qwen-inference.ps1")

    assert "$turnHasImage = [bool]$script:currentTurnHasImage" in desktop
    assert "$script:currentTurnHasImage -and" in desktop
    assert "$script:currentTurnHasImage = $false" in desktop
    assert "Remove-PreviousImagePayload $history[$i]" in desktop
    assert "if ($jobEvent.data -and $jobEvent.data.direct)" in desktop
    assert "An image is attached to the chat." not in desktop
    assert "An image was attached to the chat;" not in inference


def test_powershell_scripts_parse():
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        pytest.skip("PowerShell is unavailable")
    scripts = sorted(path.relative_to(ROOT)
                     for directory in (ROOT, ROOT / "agent", ROOT / "scripts")
                     for path in directory.glob("*.ps1"))
    with tempfile.TemporaryDirectory() as temp_dir:
        parse_paths = []
        for script in scripts:
            path = Path(temp_dir) / Path(script).name
            path.write_bytes((ROOT / script).read_bytes())
            parse_paths.append(path)

        for path in parse_paths:
            command = (
                "$tokens = $null; $parseErrors = $null; "
                "$source = [System.IO.File]::ReadAllText("
                + "'"
                + str(path).replace("'", "''")
                + "'); "
                + "$null = [System.Management.Automation.Language.Parser]::ParseInput($source, [ref]$tokens, [ref]$parseErrors); "
                + "if ($parseErrors.Count) { throw ($parseErrors | Out-String) }; "
            )
            result = subprocess.run(
                [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True,
                # PowerShell parse errors are localized per the OS UI language (a real
                # mojibake was hit on a non-English Windows in this session);
                # without an explicit encoding the system code page is used and the error
                # text may fail to decode, leaving stdout/stderr as None.
                encoding="utf-8", errors="replace",
                check=False,
            )
            assert result.returncode == 0, f"{path}: {result.stderr or result.stdout}"


def test_runtime_ocr_intent_matrix():
    # Needs PowerShell 7; conftest adds the installed runtime\pwsh to PATH.
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")

    # qwen-inference.ps1 derives $root from its own location; it is dot-sourced through ROOT.
    script = r"""
$job = @{ messages = @(@{ role = 'user'; content = 'Read the text in the image' }); compactOnly = $true; provider = 'local'; model = 'test'; contextLimit = 32768; maxTokens = 1200; agent = $false; internet = $false; office = $false; documentCreate = $false }
$shared = @{ Cancel = $false }
. '__ROOT__\agent\qwen-inference.ps1' $job $shared | Out-Null
foreach ($text in @('Read the text in the image', 'Explain what you read in this image', 'Explain the text in the image', "What's in the image", 'Read the text in the image and state the confidence score')) {
    Write-Output (('OCR=' + [bool](Test-OcrIntent $text)) + '|EXPLAIN=' + [bool](Test-OcrExplainIntent $text))
}
""".replace("__ROOT__", str(ROOT))

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".ps1", encoding="utf-8-sig", delete=False
    ) as handle:
        handle.write(script)
        script_path = Path(handle.name)
    try:
        result = subprocess.run(
            [str(pwsh), "-NoProfile", "-NonInteractive", "-File", str(script_path)],
            capture_output=True,
            # pwsh writes UTF-8; without an explicit encoding the Windows system code page is used
            # and on a non-UTF-8 locale (e.g. cp1254) a decoding error leaves stdout as None
            # (see tests/test_desktop_behavior.py -> the same fix as run_ps).
            encoding="utf-8", errors="replace",
            check=False,
        )
    finally:
        script_path.unlink(missing_ok=True)
    assert result.returncode == 0, result.stderr or result.stdout
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    assert lines == [
        "OCR=True|EXPLAIN=False",
        "OCR=True|EXPLAIN=True",
        "OCR=True|EXPLAIN=True",
        "OCR=False|EXPLAIN=False",
        "OCR=True|EXPLAIN=True",
    ]


def test_multi_image_ocr_is_an_explicit_one_turn_mode():
    desktop = read_text("qwen-desktop.ps1")
    inference = read_text("agent/qwen-inference.ps1")

    assert "$script:multiImageOcrMode = $false" in desktop
    assert "function Test-MultiImageOcrActivationIntent" in desktop
    assert "if ($activationIntent -and $allPaths.Count -eq 0)" in desktop
    assert "$multiImageRequested = $script:multiImageOcrMode -and $script:selectedImages.Count -gt 1" in desktop
    assert "First send the message \"turn on multi-image reading mode\"" in desktop
    assert "multiImageOcrMode = [bool]$script:multiImageOcrMode" in desktop
    assert "$script:multiImageOcrMode = $false" in desktop[desktop.index("'done'"):]
    assert "Multi-image reading on — you can upload several images." in desktop
    assert "$multiImageOcrRequested = [bool]$JobData.multiImageOcrMode" in inference
    assert "function Invoke-ImageOcrWithModelSwap([string[]]$paths, [bool]$waitForRestore = $true)" in inference
    assert "The local model is loading; the image/chat will continue when it is ready" in inference
    assert "if (!$script:multiImageOcrMode -and $script:selectedImages.Count -ge 1)" in desktop


def test_generated_artifacts_can_be_opened_directly():
    """A generated file (image/video/document) must be openable from the chat bubble in one click."""
    desktop = read_text("qwen-desktop.ps1")
    assert "function Open-BubbleArtifact" in desktop
    assert "Open-BubbleArtifact $this.Tag" in desktop
    assert "Start-Process -FilePath $path" in desktop
    assert "openFile = $null" in desktop
    assert "$openFile.Text" in desktop


def test_update_check_uses_a_github_token_for_the_private_repo():
    """On a private repo releases/latest returns 404; the update check/download must use a
    GitHub token (GITHUB_TOKEN/GH_TOKEN environment variable or the stored 'github' key)."""
    catalog = read_text("agent/profile-catalog.ps1")
    assert "function Get-UpdateToken" in catalog
    assert "GITHUB_TOKEN" in catalog and "GH_TOKEN" in catalog
    assert "Authorization" in catalog
    assert "Bearer $token" in catalog