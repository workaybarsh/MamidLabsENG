#Requires -Version 5.1
<#
.SYNOPSIS
    End-to-end smoke test after installation: chat, document, offline tools, network switches, history resume, OCR, image, video.

.DESCRIPTION
    install-mamidlabs.ps1 only prepares the infrastructure (binaries, environments);
    the model files are downloaded separately from the Profiles menu inside the app.
    This script runs after that step: it loads the real model, sends a real request,
    and proves every component works by producing a real document/image/video. It
    closes the gap between "it is in the source" and "it is proven to work" (see the
    README "Known notes" and the "Post-install smoke test" item of the installation
    analysis).

    Every step is independent: if one fails the others are still attempted, and a
    summary of all of them is reported at the end. A non-zero exit code means at least
    one step failed.

.EXAMPLE
    pwsh -File scripts\smoke-test.ps1 -Profile lite
    Chat + document + image + video, all of them.

.EXAMPLE
    pwsh -File scripts\smoke-test.ps1 -Profile lite -SkipVideo -SkipImage
    Only chat and document; for a quick check.

.NOTES
    The image step unloads and reloads the language model just like the real app;
    then the "post-media switch" step measures that the language model came back and runs
    without VRAM overflow (without a speed drop). This is the verification of the model
    switch after an image on the 8 GB LITE card. The result is also written to
    logs\smoke-test-last.json (shown in the setup wizard summary).
#>
param(
    [ValidateSet('max', 'flash', 'lite')][string]$Profile = 'lite',
    [string]$Root = (Join-Path $env:LOCALAPPDATA 'Programs\MamidLabs'),
    [switch]$SkipChat,
    [switch]$SkipDocument,
    [switch]$SkipImage,
    [switch]$SkipVideo,
    [switch]$SkipOcr,
    [switch]$SkipEdit,
    # Skips the offline tools step (when applying a network block is not wanted).
    [switch]$SkipOffline,
    # Skips the network-switch (online<->offline) step.
    [switch]$SkipNetworkSwitch,
    # Skips the resume-from-history (close -> reopen -> load -> continue) step.
    [switch]$SkipHistoryResume,
    # If the post-media chat speed drops below this ratio (relative to the first measurement) it counts as VRAM overflow.
    [double]$MinSpeedRatio = 0.7,
    [string]$ReportPath
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
if (!(Get-Command Start-ThreadJob -ErrorAction SilentlyContinue)) { Import-Module ThreadJob -ErrorAction Stop }

$manage = Join-Path $Root 'scripts\manage-qwen.ps1'
$results = [ordered]@{}
$lite = ($Profile -eq 'lite')
$script:chatBaseline = $null
$script:lastImage = $null
if (!$ReportPath) { $ReportPath = Join-Path $Root 'logs\smoke-test-last.json' }

function Write-Section([string]$text) { Write-Host ''; Write-Host ('== ' + $text) -ForegroundColor Cyan }
function Report([string]$name, [bool]$ok, [string]$detail) {
    $results[$name] = @{ ok = $ok; detail = $detail; skipped = $false }
    $mark = if ($ok) { '[PASS] ' } else { '[FAIL] ' }
    $color = if ($ok) { 'Green' } else { 'Red' }
    Write-Host ("   $mark $name - $detail") -ForegroundColor $color
}

function Wait-ForHealth([int]$timeoutSeconds) {
    $deadline = [DateTime]::UtcNow.AddSeconds($timeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $health = Invoke-RestMethod 'http://127.0.0.1:8080/health' -TimeoutSec 2 -ErrorAction Stop
            if ($health.status -eq 'ok') { return $true }
        } catch { }
        Start-Sleep -Milliseconds 1000
    }
    return $false
}

# Skipped steps show up in the summary but do not count as failures.
function Skip([string]$name, [string]$reason) {
    $results[$name] = @{ ok = $true; detail = $reason; skipped = $true }
    Write-Host ("   [skip]  $name - $reason") -ForegroundColor DarkGray
}

function Get-FreeVramMiB {
    $smi = (Get-Command nvidia-smi -ErrorAction SilentlyContinue).Source
    if (!$smi) { return $null }
    try { return [int](((& $smi --query-gpu=memory.free --format=csv,noheader,nounits) | Select-Object -First 1) -replace '\D', '') } catch { return $null }
}

function Test-ComfyRunning {
    return [bool](Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*ComfyUI\main.py*' } | Select-Object -First 1)
}

# Sends a single chat request; returns the content, token count, duration and the
# generation speed measured by the server (timings.predicted_per_second).
function Invoke-ChatProbe {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    # reasoning_effort='low' + chat_template_kwargs bring it into the same shape as the real
    # generation request (Set-LowEffortRequest in qwen-inference.ps1).
    # Without this, Lite's server-side enforced thinking budget (300 tokens) could
    # use up max_tokens by itself and cut off before producing real content -
    # exactly what was caught on a real machine: with max_tokens=200 the model spent
    # all 200 tokens thinking and gave an "empty answer" error.
    $body = @{
        model = "smoke-$Profile"
        messages = @(@{ role = 'user'; content = 'Introduce yourself in one sentence.' })
        max_tokens = 600
        reasoning_effort = 'low'
        chat_template_kwargs = @{ reasoning_effort = 'low' }
    } | ConvertTo-Json -Depth 5
    $response = Invoke-RestMethod 'http://127.0.0.1:8080/v1/chat/completions' -Method Post `
        -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 180
    $content = [string]$response.choices[0].message.content
    if ([string]::IsNullOrWhiteSpace($content)) { throw 'The model returned an empty answer (the thinking budget may have run out).' }
    $tokens = [int]$response.usage.completion_tokens
    $tps = if ($response.timings -and $response.timings.predicted_per_second) { [double]$response.timings.predicted_per_second }
           elseif ($sw.Elapsed.TotalSeconds -gt 0) { $tokens / $sw.Elapsed.TotalSeconds } else { 0 }
    return @{ content = $content; tokens = $tokens; seconds = [int]$sw.Elapsed.TotalSeconds; tps = [math]::Round($tps, 1) }
}

# ------------------------------------------------------------------- chat
Write-Section 'Chat model'
if ($SkipChat) {
    Write-Host '   [skip] -SkipChat given'
} else {
    try {
        & $manage -Action stop | Out-Null
        & $manage -Action start -Profile $Profile | Out-Null
        if (!(Wait-ForHealth 120)) { throw 'The model was not ready in 120 seconds.' }
        $probe = Invoke-ChatProbe
        $script:chatBaseline = $probe
        Report 'chat' $true ("$($probe.tokens) tokens, $($probe.seconds) s, $($probe.tps) tok/s, answer: " + $probe.content.Substring(0, [Math]::Min(60, $probe.content.Length)))
    } catch {
        Report 'chat' $false $_.Exception.Message
    }
}

# ------------------------------------------------------------------- document
Write-Section 'Document reading (a real .docx)'
if ($SkipDocument) {
    Write-Host '   [skip] -SkipDocument given'
} else {
    $python = Join-Path $Root 'venv\app\Scripts\python.exe'
    $docPath = Join-Path $env:TEMP 'mamidlabs-smoke-test.docx'
    $marker = 'MAMIDLABS-SMOKE-' + (Get-Random -Maximum 999999)
    try {
        if (!(Test-Path -LiteralPath $python -PathType Leaf)) { throw "No app environment: $python" }
        & $python -c "from docx import Document; d = Document(); d.add_paragraph('$marker'); d.save(r'$docPath')"
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the test document (python-docx).' }
        # office-tools.py's ACTIONS table has no action named 'read_document'
        # (only read_docx/read_pdf/...; 'read_document' is only the tool NAME inside
        # qwen-inference.ps1 - it is mapped to the right action by extension). Caught on a
        # real machine: the wrong action name gave an "unknown action" error.
        $job = @{ action = 'read_docx'; path = $docPath } | ConvertTo-Json -Compress
        $out = $job | & $python (Join-Path $Root 'agent\office-tools.py') 2>&1 | Out-String
        if ($out -notmatch [regex]::Escape($marker)) { throw "The marker text was not found in the content read. Output: $($out.Substring(0, [Math]::Min(200, $out.Length)))" }
        Report 'document' $true 'The created .docx was read back correctly'
    } catch {
        Report 'document' $false $_.Exception.Message
    } finally {
        Remove-Item -LiteralPath $docPath -Force -ErrorAction SilentlyContinue
    }
}

# ------------------------------------------------------------- offline
# Network drop while the internet chip is on (hotspot, DNS timeout): a web tool
# must return valid error JSON instead of raising. This scenario fed the recurring
# "An existing connection was forcibly closed by the remote host" error on a real
# machine; the tool layer no longer crashes. Network access is cut with a black-hole
# proxy (temporarily, so the user's setting is not disturbed).
Write-Section 'Offline tools'
if ($SkipOffline) {
    Write-Host '   [skip] -SkipOffline given'
} else {
    $python = Join-Path $Root 'venv\app\Scripts\python.exe'
    try {
        if (!(Test-Path -LiteralPath $python -PathType Leaf)) { throw "app environment missing: $python" }
        $savedHttp = $env:HTTP_PROXY; $savedHttps = $env:HTTPS_PROXY
        $env:HTTP_PROXY = 'http://127.0.0.1:9'; $env:HTTPS_PROXY = 'http://127.0.0.1:9'
        try {
            $payload = @{ action = 'search_web'; query = 'offline smoke test'; count = 2 } | ConvertTo-Json -Compress
            $out = $payload | & $python (Join-Path $Root 'agent\qwen-agent-tools.py') 2>&1 | Out-String
            $code = $LASTEXITCODE
        } finally {
            $env:HTTP_PROXY = $savedHttp; $env:HTTPS_PROXY = $savedHttps
        }
        if ($code -ne 0) { throw "Tool crashed (exit code $code): $($out.Substring(0, [Math]::Min(200, $out.Length)))" }
        $parsed = $out | ConvertFrom-Json
        if (!$parsed.error) { throw "No error JSON while offline: $($out.Substring(0, [Math]::Min(200, $out.Length)))" }
        Report 'offline' $true 'Web tool returned valid error JSON while offline without crashing'
    } catch {
        Report 'offline' $false $_.Exception.Message
    }
}

# ----------------------------------------------- network switches (online <-> offline)
# User complaint: the network dropping while online, or going up and down mid-conversation;
# even when the answer was produced, "[Error] ... forcibly closed" was appended below it.
# The network state is changed across successive turns; the local worker must not emit an
# 'error' event in any turn. Network access is cut with a black-hole proxy and restored
# afterwards so the user's setting is not disturbed.
Write-Section 'Network switches (online <-> offline)'
if ($SkipNetworkSwitch) {
    Write-Host '   [skip] -SkipNetworkSwitch given'
} elseif (!(Wait-ForHealth 5)) {
    Skip 'network_switch' 'the local model is not ready (the chat step may not have run)'
} else {
    $savedHttp = $env:HTTP_PROXY; $savedHttps = $env:HTTPS_PROXY
    try {
        $plan = @(
            @{ label = 'online';  online = $true;  prompt = 'Introduce yourself in one sentence.' }
            @{ label = 'offline'; online = $false; prompt = 'What is the capital of Turkey? Answer briefly.' }
            @{ label = 'online';  online = $true;  prompt = 'What is the Einstein riddle? Explain briefly.' }
        )
        $turnErrors = 0; $seen = @()
        foreach ($step in $plan) {
            if ($step.online) { Remove-Item Env:HTTP_PROXY, Env:HTTPS_PROXY -ErrorAction SilentlyContinue }
            else { $env:HTTP_PROXY = 'http://127.0.0.1:9'; $env:HTTPS_PROXY = 'http://127.0.0.1:9' }
            $jobData = @{
                root = $Root
                messages = @(@{ role = 'user'; content = $step.prompt })
                provider = 'local'; model = "smoke-$Profile"; profile = $Profile
                maxTokens = 500; contextLimit = 32768; boundFolder = $null
                consultProviders = @{}; attachedDocuments = @(); currentImagePaths = @()
                hasCurrentImage = $false; multiImageOcr = $false; multiImageOcrMode = $false
                internet = $true; agent = $false; office = $false; documentCreate = $false; effort = 'low'
            }
            $shared = [hashtable]::Synchronized(@{ Cancel = $false; Cts = $null; AccessMode = 'full'
                AskTool = $null; AskDetail = $null; AskAnswer = $null; AskId = $null; AskDeadline = $null })
            $job = Start-ThreadJob -FilePath (Join-Path $Root 'agent\qwen-inference.ps1') -ArgumentList $jobData, $shared
            $items = @(Receive-Job -Job $job -Wait)
            Remove-Job -Job $job -Force
            $err = @($items | Where-Object { $_.kind -eq 'error' })
            if ($err.Count) { $turnErrors += $err.Count; $seen += ($step.label + ':' + [string]$err[0].text) }
        }
        if ($turnErrors -gt 0) {
            Report 'network_switch' $false ("$turnErrors errors -> " + ($seen -join ' | '))
        } else {
            Report 'network_switch' $true 'No turn errored while the network went online/offline'
        }
    } catch {
        Report 'network_switch' $false $_.Exception.Message
    } finally {
        $env:HTTP_PROXY = $savedHttp; $env:HTTPS_PROXY = $savedHttps
    }
}

# ------------------------------------------- resume from history (close -> reopen)
# User scenario: close the program MID-CONVERSATION, reopen it, load the chat from
# history and continue. The chat is stored as the model-facing message list, encrypted
# with DPAPI (Save-Chat); on reopen it is loaded the same way (Load-Chat) and the
# continuation turn is sent with that history. This step runs the full serialization
# round-trip plus a continuation turn; no turn may error.
Write-Section 'Resume from history (close -> reopen)'
if ($SkipHistoryResume) {
    Write-Host '   [skip] -SkipHistoryResume given'
} elseif (!(Wait-ForHealth 5)) {
    Skip 'history_resume' 'the local model is not ready (the chat step may not have run)'
} else {
    try {
        Add-Type -AssemblyName System.Security.Cryptography.ProtectedData -ErrorAction SilentlyContinue
        $prefix = 'MAMIDLABS-CHAT-DPAPI-V2'
        $chatFile = Join-Path $env:TEMP 'mamidlabs-smoke-reload.json'
        $turns = @(
            @{ role = 'user'; content = 'What is the Einstein riddle? Explain briefly.' }
        )
        # 1) First turn: a real request that may also trigger a tool call.
        $jobData = @{
            root = $Root; messages = @($turns)
            provider = 'local'; model = "smoke-$Profile"; profile = $Profile
            maxTokens = 400; contextLimit = 32768; boundFolder = $null
            consultProviders = @{}; attachedDocuments = @(); currentImagePaths = @()
            hasCurrentImage = $false; multiImageOcr = $false; multiImageOcrMode = $false
            internet = $true; agent = $false; office = $false; documentCreate = $false; effort = 'low'
        }
        $shared = [hashtable]::Synchronized(@{ Cancel = $false; Cts = $null; AccessMode = 'full'
            AskTool = $null; AskDetail = $null; AskAnswer = $null; AskId = $null; AskDeadline = $null })
        $job = Start-ThreadJob -FilePath (Join-Path $Root 'agent\qwen-inference.ps1') -ArgumentList $jobData, $shared
        $items = @(Receive-Job -Job $job -Wait); Remove-Job -Job $job -Force
        $err1 = @($items | Where-Object { $_.kind -eq 'error' })
        if ($err1.Count) { throw "The first turn errored: $($err1[0].text)" }
        $done = @($items | Where-Object { $_.kind -eq 'done' }) | Select-Object -Last 1
        if (!$done) { throw 'The first turn produced no done event.' }
        # Persistent message list: the user message plus the tool exchange from done.
        $chatMessages = @($turns) + @($done.data)
        # 2) Save like Save-Chat (DPAPI).
        $payload = @{ id = 'smoketest000000000000000000000000'; title = 'smoke-reload'; messages = @($chatMessages); transcript = @() }
        $json = $payload | ConvertTo-Json -Depth 30
        $cipher = [Security.Cryptography.ProtectedData]::Protect([Text.Encoding]::UTF8.GetBytes($json), $null, [Security.Cryptography.DataProtectionScope]::CurrentUser)
        [IO.File]::WriteAllText($chatFile, ($prefix + "`n" + [Convert]::ToBase64String($cipher)), [Text.UTF8Encoding]::new($false))
        # 3) Load like Load-Chat.
        $raw = Get-Content -LiteralPath $chatFile -Raw -Encoding UTF8
        $plain = [Text.Encoding]::UTF8.GetString([Security.Cryptography.ProtectedData]::Unprotect(
            [Convert]::FromBase64String($raw.Substring($prefix.Length + 1).Trim()), $null, [Security.Cryptography.DataProtectionScope]::CurrentUser))
        $reloaded = @(($plain | ConvertFrom-Json).messages)
        if ($reloaded.Count -lt 2) { throw "The reloaded history is incomplete ($($reloaded.Count) messages)." }
        # 4) Continuation turn: add a new user message to the reloaded history.
        $jobData2 = $jobData.Clone(); $jobData2.messages = @($reloaded) + @(@{ role = 'user'; content = 'Now summarize that in one sentence.' })
        $shared2 = [hashtable]::Synchronized(@{ Cancel = $false; Cts = $null; AccessMode = 'full'
            AskTool = $null; AskDetail = $null; AskAnswer = $null; AskId = $null; AskDeadline = $null })
        $job2 = Start-ThreadJob -FilePath (Join-Path $Root 'agent\qwen-inference.ps1') -ArgumentList $jobData2, $shared2
        $items2 = @(Receive-Job -Job $job2 -Wait); Remove-Job -Job $job2 -Force
        $err2 = @($items2 | Where-Object { $_.kind -eq 'error' })
        if ($err2.Count) { throw "The continuation turn errored: $($err2[0].text)" }
        Report 'history_resume' $true ("Saved, reloaded ($($reloaded.Count) messages) and continued; no errors")
    } catch {
        Report 'history_resume' $false $_.Exception.Message
    } finally {
        Remove-Item -LiteralPath (Join-Path $env:TEMP 'mamidlabs-smoke-reload.json') -Force -ErrorAction SilentlyContinue
    }
}

# --------------------------------------------------------------------- OCR
Write-Section 'OCR (GPU image + CPU PDF)'
if ($SkipOcr) {
    Write-Host '   [skip] -SkipOcr given'
} else {
    $ocrPython = Join-Path $Root 'venv\ocr\Scripts\python.exe'
    $ocrCpuPython = Join-Path $Root 'venv\ocr-cpu\Scripts\python.exe'
    $imgPath = Join-Path $env:TEMP 'mamidlabs-smoke-ocr.png'
    $pdfPath = Join-Path $env:TEMP 'mamidlabs-smoke-ocr.pdf'
    $marker = 'MAMIDLABSOCR' + (Get-Random -Maximum 9999)
    try {
        if (!(Test-Path -LiteralPath $ocrPython -PathType Leaf)) { throw "No ocr environment: $ocrPython. Run scripts\install-ocr.ps1 first." }
        # A clear, large-font test image: with System.Drawing, no extra dependency needed.
        Add-Type -AssemblyName System.Drawing
        $bmp = New-Object System.Drawing.Bitmap(500, 120)
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        $g.Clear([System.Drawing.Color]::White)
        $font = New-Object System.Drawing.Font('Arial', 28, [System.Drawing.FontStyle]::Bold)
        $g.DrawString($marker, $font, [System.Drawing.Brushes]::Black, 10, 30)
        $g.Dispose()
        $bmp.Save($imgPath, [System.Drawing.Imaging.ImageFormat]::Png)
        $bmp.Dispose()

        # -- GPU: plain image --
        $reqImage = @{ action = 'ocr_image'; path = $imgPath } | ConvertTo-Json -Compress
        $outImage = $reqImage | & $ocrPython (Join-Path $Root 'agent\ocr-tools.py') | Out-String
        $parsedImage = $outImage | ConvertFrom-Json
        if ($parsedImage.error) { throw "GPU OCR error: $($parsedImage.error)" }
        if ($parsedImage.text -notmatch [regex]::Escape($marker.Substring(0, 8))) {
            throw "GPU OCR did not find the expected text. Output: $($parsedImage.text)"
        }

        # -- CPU: scanned PDF (no native text layer, the page is OCRed as an image) --
        & $ocrCpuPython -c "import fitz; d = fitz.open(); p = d.new_page(width=500, height=120); p.insert_image(fitz.Rect(0, 0, 500, 120), filename=r'$imgPath'); d.save(r'$pdfPath')"
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the test PDF (PyMuPDF).' }
        $reqPdf = @{ action = 'ocr_pdf'; path = $pdfPath } | ConvertTo-Json -Compress
        $outPdf = $reqPdf | & $ocrCpuPython (Join-Path $Root 'agent\ocr-tools.py') | Out-String
        $parsedPdf = $outPdf | ConvertFrom-Json
        if ($parsedPdf.error) { throw "CPU PDF OCR error: $($parsedPdf.error)" }
        if ($parsedPdf.text -notmatch [regex]::Escape($marker.Substring(0, 8))) {
            throw "CPU PDF OCR did not find the expected text. Output: $($parsedPdf.text)"
        }

        Report 'ocr' $true ("GPU confidence $([math]::Round($parsedImage.confidence,3)), CPU confidence $([math]::Round($parsedPdf.confidence,3))")
    } catch {
        Report 'ocr' $false $_.Exception.Message
    } finally {
        Remove-Item -LiteralPath $imgPath, $pdfPath -Force -ErrorAction SilentlyContinue
    }
}

# ------------------------------------------------------------------ image
Write-Section 'Image generation'
if ($SkipImage) {
    Write-Host '   [skip] -SkipImage given'
} else {
    try {
        # Lite's GGUF quantized Krea-2 lives under models\unet, where the ComfyUI-GGUF custom
        # node looks for it; the full-precision version of MAX/FLASH lives under diffusion_models.
        $checkpointDir = if ($lite) { Join-Path $Root 'ComfyUI\models\unet' } else { Join-Path $Root 'ComfyUI\models\diffusion_models' }
        $pattern = '*krea2*'
        if (!(Get-ChildItem -LiteralPath $checkpointDir -Filter $pattern -File -ErrorAction SilentlyContinue)) {
            throw "Image model not found ($pattern in $checkpointDir). Download it from the Profiles menu first."
        }
        $jobData = @{
            root = $Root
            prompt = 'A simple test image: blue sky and green grass'
            # If the chat step ran, the language model is unloaded and reloaded as in the real app.
            aspect = '1:1'; restoreProfile = $(if ($script:chatBaseline) { $Profile } else { '' }); mode = 'new'; strength = 0.6
            sourceImage = $null; realism = $false; lite = $lite
        }
        $shared = [hashtable]::Synchronized(@{ Cancel = $false; Process = $null })
        $sw = [Diagnostics.Stopwatch]::StartNew()
        $job = Start-ThreadJob -FilePath (Join-Path $Root 'agent\imagegen-run.ps1') -ArgumentList $jobData, $shared
        $items = Receive-Job -Job $job -Wait
        Remove-Job -Job $job -Force
        $imageEvent = $items | Where-Object { $_.kind -eq 'image' } | Select-Object -Last 1
        $errorEvent = $items | Where-Object { $_.kind -eq 'error' } | Select-Object -Last 1
        if ($errorEvent) { throw $errorEvent.text }
        if (!$imageEvent -or !(Test-Path -LiteralPath $imageEvent.text -PathType Leaf)) { throw 'The output file was not created.' }
        $size = (Get-Item -LiteralPath $imageEvent.text).Length
        if ($size -lt 1024) { throw "The output file is too small ($size bytes); it may be corrupt." }
        $script:lastImage = [string]$imageEvent.text
        Report 'image' $true ("$([int]$sw.Elapsed.TotalSeconds) s, " + [IO.Path]::GetFileName($imageEvent.text) + " ($([math]::Round($size/1KB)) KB)")
    } catch {
        Report 'image' $false $_.Exception.Message
    }
}

# ------------------------------------------------------- photo editing
Write-Section 'Photo editing (Krea 2 Identity Edit)'
$editLora = Join-Path $Root 'ComfyUI\models\loras\krea2_identity_edit_v1_2.safetensors'
$editNode = Join-Path $Root 'ComfyUI\custom_nodes\comfyui-krea2edit\__init__.py'
if ($SkipEdit -or $SkipImage) {
    Skip 'edit' '-SkipEdit/-SkipImage given'
} elseif ($lite) {
    # identity_edit is not offered on 8 GB VRAM: the GGUF diffusion model + the text/image encoder do not
    # fit in VRAM together and it goes up to 641-884 s (measured, see the hardware table in AGENTS.md).
    Skip 'edit' 'not supported on the LITE profile (8 GB VRAM: extremely slow, ~11-15 min)'
} elseif (!(Test-Path -LiteralPath $editNode -PathType Leaf)) {
    Skip 'edit' 'comfyui-krea2edit is not installed (install-mamidlabs.ps1 -Stage comfyui)'
} elseif (!(Test-Path -LiteralPath $editLora -PathType Leaf)) {
    Skip 'edit' 'no Identity Edit LoRA (Profile manager)'
} elseif (!$script:lastImage) {
    Skip 'edit' 'the image step produced no output'
} else {
    try {
        $jobData = @{
            root = $Root; prompt = 'Change the sky to a warm sunset.'
            aspect = '1:1'; restoreProfile = $(if ($script:chatBaseline) { $Profile } else { '' })
            mode = 'identity_edit'; strength = 0.6; sourceImage = $script:lastImage; realism = $false; lite = $lite
        }
        $shared = [hashtable]::Synchronized(@{ Cancel = $false; Process = $null })
        $sw = [Diagnostics.Stopwatch]::StartNew()
        $job = Start-ThreadJob -FilePath (Join-Path $Root 'agent\imagegen-run.ps1') -ArgumentList $jobData, $shared
        $items = Receive-Job -Job $job -Wait
        Remove-Job -Job $job -Force
        $imageEvent = $items | Where-Object { $_.kind -eq 'image' } | Select-Object -Last 1
        $errorEvent = $items | Where-Object { $_.kind -eq 'error' } | Select-Object -Last 1
        if ($errorEvent) { throw $errorEvent.text }
        if (!$imageEvent -or !(Test-Path -LiteralPath $imageEvent.text -PathType Leaf)) { throw 'The edited image was not created.' }
        Report 'edit' $true ("$([int]$sw.Elapsed.TotalSeconds) s, " + [IO.Path]::GetFileName($imageEvent.text))
    } catch {
        Report 'edit' $false $_.Exception.Message
    }
}

# --------------------------------------------------- post-media switch
# After an image job the language model must be reloaded and fit entirely in VRAM. On the
# 8 GB LITE card, if ComfyUI's idle CUDA context (~300 MiB) pushes the model into shared
# memory the chat speed drops noticeably; this step catches that.
Write-Section 'Post-media model switch'
if (!$script:chatBaseline -or !$script:lastImage) {
    Skip 'post_media_switch' 'both the chat and the image steps must succeed'
} else {
    try {
        if (!(Wait-ForHealth 180)) { throw 'The language model did not come back within 180 seconds after the image.' }
        $probe = Invoke-ChatProbe
        $ratio = if ($script:chatBaseline.tps -gt 0) { $probe.tps / $script:chatBaseline.tps } else { 1 }
        $free = Get-FreeVramMiB
        $comfyState = if (Test-ComfyRunning) { 'ComfyUI open (models unloaded)' } else { 'ComfyUI closed' }
        $detail = "$($probe.tps) tok/s (first measurement $($script:chatBaseline.tps), ratio $([math]::Round($ratio, 2))), free VRAM $(if ($null -ne $free) { "$free MiB" } else { 'not measured' }), $comfyState"
        if ($ratio -lt $MinSpeedRatio) {
            throw "Chat speed dropped: $detail. There may be a VRAM overflow; see the logs\qwen-$Profile.stderr.log file."
        }
        Report 'post_media_switch' $true $detail
    } catch {
        Report 'post_media_switch' $false $_.Exception.Message
    }
}

# -------------------------------------------------------------------- video
Write-Section 'Video generation'
if ($SkipVideo) {
    Write-Host '   [skip] -SkipVideo given'
} else {
    try {
        $videoDir = Join-Path $Root 'ComfyUI\models\diffusion_models'
        if (!(Get-ChildItem -LiteralPath $videoDir -Filter '*wan2.2*ti2v*' -File -ErrorAction SilentlyContinue)) {
            throw "Video model not found (wan2.2*ti2v* in $videoDir). Download it from the Profiles menu first."
        }
        $jobData = @{
            root = $Root
            prompt = 'Simple test: a still puddle of water'
            mode = 'text'; aspect = '16:9'; seconds = 3.0; quality = ''
            sourceFile = $null; restoreProfile = ''; lite = $lite
        }
        $shared = [hashtable]::Synchronized(@{ Cancel = $false; Process = $null })
        $sw = [Diagnostics.Stopwatch]::StartNew()
        $job = Start-ThreadJob -FilePath (Join-Path $Root 'agent\videogen-run.ps1') -ArgumentList $jobData, $shared
        $items = Receive-Job -Job $job -Wait
        Remove-Job -Job $job -Force
        $videoEvent = $items | Where-Object { $_.kind -eq 'video' } | Select-Object -Last 1
        $errorEvent = $items | Where-Object { $_.kind -eq 'error' } | Select-Object -Last 1
        if ($errorEvent) { throw $errorEvent.text }
        if (!$videoEvent -or !(Test-Path -LiteralPath $videoEvent.text -PathType Leaf)) { throw 'The output file was not created.' }
        $size = (Get-Item -LiteralPath $videoEvent.text).Length
        if ($size -lt 1024) { throw "The output file is too small ($size bytes); it may be corrupt." }
        Report 'video' $true ("$([int]$sw.Elapsed.TotalSeconds) s, " + [IO.Path]::GetFileName($videoEvent.text) + " ($([math]::Round($size/1KB)) KB)")
    } catch {
        Report 'video' $false $_.Exception.Message
    }
}

# -------------------------------------------------------------------- summary
Write-Section 'Summary'
$failed = @($results.Keys | Where-Object { !$results[$_].ok })
foreach ($name in $results.Keys) {
    $entry = $results[$name]
    $mark = if ($entry.skipped) { '[SKIP] ' } elseif ($entry.ok) { '[PASS] ' } else { '[FAIL] ' }
    $color = if ($entry.skipped) { 'DarkGray' } elseif ($entry.ok) { 'Green' } else { 'Red' }
    Write-Host ("   $mark $name") -ForegroundColor $color
}
# Machine-readable report for the setup wizard and support.
try {
    $gpu = $null
    $smi = (Get-Command nvidia-smi -ErrorAction SilentlyContinue).Source
    if ($smi) { $gpu = ((& $smi --query-gpu=name,memory.total --format=csv,noheader) | Select-Object -First 1) }
    $report = [ordered]@{
        time = (Get-Date).ToString('s'); profile = $Profile; gpu = $gpu
        ok = ($failed.Count -eq 0); results = $results
    }
    New-Item -ItemType Directory -Path (Split-Path -Parent $ReportPath) -Force | Out-Null
    [IO.File]::WriteAllText($ReportPath, ($report | ConvertTo-Json -Depth 6), [Text.UTF8Encoding]::new($false))
    Write-Host ''
    Write-Host ("   Report: $ReportPath") -ForegroundColor DarkGray
} catch {
    Write-Host ("   Could not write the report: " + $_.Exception.Message) -ForegroundColor Yellow
}
if ($failed.Count) {
    Write-Host ''
    Write-Host ("$($failed.Count) components failed: " + ($failed -join ', ')) -ForegroundColor Red
    exit 1
} else {
    Write-Host ''
    Write-Host 'All components verified.' -ForegroundColor Green
    exit 0
}
