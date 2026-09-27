param([hashtable]$JobData, [hashtable]$Shared)

# Video generation flow (the same VRAM swap as image generation):
#   1) The language model is unloaded from VRAM
#   2) ComfyUI + Wan 2.2 TI2V-5B are loaded, the video is generated
#   3) The video model is closed, the language model is reloaded
#
# Modes:
#   text    : video from scratch (text-to-video)
#   image   : video from the attached image (image-to-video)
#   video   : continue from the last frame of the attached video (video continuation)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Add-Type -AssemblyName System.Drawing

# Derive the install root from this file's own location. IMPORTANT: this file is started with
# Start-ThreadJob -FilePath; that reads the target file as TEXT and runs it in a new
# runspace - $PSScriptRoot is ALWAYS EMPTY in that context (verified on a real machine).
# The primary source is the value qwen-desktop.ps1 passes explicitly via JobData.root;
# $PSScriptRoot only kicks in when this file is run directly with '&' or '-File'
# (manual testing/debugging).
$root = if ($JobData.root) { [string]$JobData.root } elseif ($PSScriptRoot) { Split-Path -Parent $PSScriptRoot } else { (Get-Location).Path }
$manage = Join-Path $root 'scripts\manage-qwen.ps1'
# The stop/restore flow of the language model is shared with imagegen-run.ps1 (Amp #17).
. (Join-Path $root 'agent\media-model-swap.ps1')
$comfyDir = Join-Path $root 'ComfyUI'
$python = Join-Path $root 'venv\imagegen\Scripts\python.exe'
$mediaPy = Join-Path $root 'agent\media-tools.py'
$comfyOut = Join-Path $root 'outputs\comfy'
$comfyIn = Join-Path $root 'outputs\input'
$videoOut = Join-Path $root 'outputs\videos'
$logs = Join-Path $root 'logs'
$comfyUrl = 'http://127.0.0.1:8188'

$negativeDefault = 'blurry, low quality, low resolution, distorted, deformed, extra limbs, watermark, text, logo, jpeg artifacts, overexposed, static image, flickering, duplicated frames'

function Emit([string]$kind, [string]$text, $data = $null) {
    [pscustomobject]@{ kind = $kind; text = $text; usage = $null; data = $data }
}

# On the Lite profile the resolution is pulled into the 480p band so 8 GB VRAM is not exceeded.
# Dimensions coming from a source image cannot exceed this limit either.
function Limit-LiteResolution([int]$width, [int]$height) {
    $maxLong = 832; $maxShort = 480
    $long = [math]::Max($width, $height); $short = [math]::Min($width, $height)
    if ($long -le $maxLong -and $short -le $maxShort) { return @($width, $height) }
    $scale = [math]::Min($maxLong / [double]$long, $maxShort / [double]$short)
    $w = [int]([math]::Round($width * $scale / 16.0) * 16)
    $h = [int]([math]::Round($height * $scale / 16.0) * 16)
    if ($w -lt 16) { $w = 16 }
    if ($h -lt 16) { $h = 16 }
    return @($w, $h)
}

function Get-Resolution([string]$aspect, [string]$quality) {
    # Scaled defaults for 16 GB VRAM; high quality takes longer.
    if ($quality -eq 'high') {
        switch ($aspect) {
            '16:9' { return @(1280, 704) }
            '9:16' { return @(704, 1280) }
            '1:1'  { return @(960, 960) }
            default { return @(1280, 704) }
        }
    }
    switch ($aspect) {
        '16:9' { return @(832, 480) }
        '9:16' { return @(480, 832) }
        '1:1'  { return @(640, 640) }
        '4:3'  { return @(768, 576) }
        default { return @(832, 480) }
    }
}

function Invoke-MediaTool([hashtable]$payload) {
    $psi = [Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $python
    $psi.ArgumentList.Add($mediaPy)
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [Text.Encoding]::UTF8
    $p = [Diagnostics.Process]::Start($psi)
    try {
        $p.StandardInput.Write(($payload | ConvertTo-Json -Compress))
        $p.StandardInput.Close()
        $out = $p.StandardOutput.ReadToEnd()
        $p.WaitForExit()
        return ($out | ConvertFrom-Json)
    } finally { $p.Dispose() }
}

function Write-SourceImage([string]$path) {
    $img = [System.Drawing.Image]::FromFile($path)
    try {
        $scale = [math]::Min(1.0, 1280.0 / [double][math]::Max($img.Width, $img.Height))
        $w = [int]([math]::Round($img.Width * $scale / 32.0) * 32)
        $h = [int]([math]::Round($img.Height * $scale / 32.0) * 32)
        if ($w -lt 128) { $w = 128 }
        if ($h -lt 128) { $h = 128 }
        $bmp = New-Object System.Drawing.Bitmap($w, $h)
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        try {
            $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
            $g.DrawImage($img, 0, 0, $w, $h)
        } finally { $g.Dispose() }
        $name = 'vref-' + (Get-Date -Format 'yyyyMMddHHmmss') + '.png'
        $bmp.Save((Join-Path $comfyIn $name), [System.Drawing.Imaging.ImageFormat]::Png)
        $bmp.Dispose()
        return @($name, $w, $h)
    } finally { $img.Dispose() }
}

function Stop-Comfy($process) {
    if ($process -and !$process.HasExited) {
        try { $process.Kill($true) } catch { try { $process.Kill() } catch { } }
        try { $process.WaitForExit(20000) | Out-Null } catch { }
    }
}

function Resolve-ComfyOutput([string]$filename, [string]$subfolder, [string]$root, [string[]]$extensions) {
    if (!$filename -or $filename.IndexOf([char]0) -ge 0) { throw 'ComfyUI returned an invalid output name.' }
    $relative = if ($subfolder) { Join-Path $subfolder $filename } else { $filename }
    try { $candidate = [IO.Path]::GetFullPath((Join-Path $root $relative)) } catch { throw 'ComfyUI returned an invalid output path.' }
    if (!(Inside $candidate $root)) { throw 'ComfyUI returned a path that escapes the output folder.' }
    if ([IO.Path]::GetExtension($candidate).ToLowerInvariant() -notin $extensions) { throw 'ComfyUI returned an unexpected video file.' }
    if (!(Test-Path -LiteralPath $candidate -PathType Leaf)) { throw 'ComfyUI output file not found.' }
    return $candidate
}

function Inside([string]$path, [string]$folder) {
    $full = [IO.Path]::GetFullPath($path).TrimEnd('\')
    $root = [IO.Path]::GetFullPath($folder).TrimEnd('\')
    return $full.Equals($root, [StringComparison]::OrdinalIgnoreCase) -or
        $full.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)
}

function Resolve-LocalModelName([string]$name) {
    if (!$name) { return $null }
    $modelRoot = Join-Path $comfyDir 'models\diffusion_models'
    try { $candidate = [IO.Path]::GetFullPath((Join-Path $modelRoot $name)) } catch { throw 'The local video model path is invalid.' }
    if (!(Inside $candidate $modelRoot) -or [IO.Path]::GetExtension($candidate).ToLowerInvariant() -ne '.safetensors') { throw 'The local video model can only be a safetensors file under diffusion_models.' }
    if (!(Test-Path -LiteralPath $candidate -PathType Leaf)) {
        # An external file added without copying (registered with ComfyUI via settings\comfy-extra-models.yaml).
        $external = Resolve-ExternalMediaModel $root $name 'diffusion_models'
        if ($external) { return $external }
        throw 'The selected local video model was not found.'
    }
    return $candidate.Substring($modelRoot.Length + 1).Replace('\', '/')
}

$comfy = $null
try {
    foreach ($dir in @($comfyOut, $comfyIn, $videoOut, $logs)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    if (!(Test-Path -LiteralPath $python -PathType Leaf)) { throw "Image/video environment not found: $python" }
    if ($JobData.localModel) { $JobData.localModel = Resolve-LocalModelName ([string]$JobData.localModel) }

    $mode = [string]$JobData.mode
    if (!$mode) { $mode = 'text' }
    $source = [string]$JobData.sourceFile

    $startImage = $null
    if ($mode -eq 'image') {
        if (!$source -or !(Test-Path -LiteralPath $source -PathType Leaf)) { throw 'This mode needs an image; attach an image to the chat first.' }
        $startImage = $source
    } elseif ($mode -eq 'video') {
        if (!$source -or !(Test-Path -LiteralPath $source -PathType Leaf)) { throw 'This mode needs a video; attach a video to the chat first.' }
        Emit 'status' 'Taking the last frame of the source video...'
        $framePath = Join-Path $comfyIn ('vframe-' + (Get-Date -Format 'yyyyMMddHHmmss') + '.png')
        $frame = Invoke-MediaTool @{ action = 'video_frame'; path = $source; which = 'last'; out = $framePath; max_side = 1280 }
        if (!$frame -or $frame.error) { throw "Could not take a frame from the video: $($frame.error)" }
        $startImage = $framePath
    }

    $lite = [bool]$JobData.lite
    $quality = [string]$JobData.quality
    if ($lite -and $quality -eq 'high') {
        Emit 'status' 'High quality video is off on the Lite profile; continuing at 480p.'
        $quality = ''
    }
    $size = Get-Resolution ([string]$JobData.aspect) $quality
    $width = $size[0]; $height = $size[1]
    $refName = $null
    if ($startImage) {
        $written = Write-SourceImage $startImage
        $refName = $written[0]
        $width = $written[1]; $height = $written[2]
    }
    if ($lite) {
        $capped = Limit-LiteResolution $width $height
        $width = $capped[0]; $height = $capped[1]
    }

    $seed = if ($JobData.seed) { [long]$JobData.seed } else { [long](Get-Random -Minimum 1 -Maximum 2147483646) }
    $steps = if ($JobData.steps) { [int]$JobData.steps } else { 20 }
    $fps = if ($JobData.fps) { [int]$JobData.fps } else { 24 }
    $seconds = if ($JobData.seconds) { [double]$JobData.seconds } else { 2.0 }
    if ($seconds -lt 1) { $seconds = 1 }
    # On 8 GB VRAM anything above 3 seconds overflows during VAE decoding.
    $maxSeconds = if ($lite) { 3 } else { 5 }
    if ($seconds -gt $maxSeconds) { $seconds = $maxSeconds }
    # The Comfy-Org repository has no fp8 version of ti2v_5B; Lite uses the same fp16 file
    # too and fits into VRAM layer by layer with --lowvram. Kijai's fp8 weights are for
    # his own wrapper nodes; if wanted they can be chosen from the Image and video API window.
    $videoModel = if ($JobData.localModel) { [string]$JobData.localModel } else { 'wan2.2_ti2v_5B_fp16.safetensors' }
    # On Lite the tile shrinks: peak VRAM drops, in exchange a slight blotch may be seen at
    # the tile boundaries. On the 16 GB profile the wide overlap is kept as is.
    $tile = if ($lite) { @{ size = 256; overlap = 64; temporal = 12; temporalOverlap = 4 } }
            else { @{ size = 320; overlap = 80; temporal = 16; temporalOverlap = 8 } }
    # length must be 4n+1
    $length = [int]([math]::Round(($seconds * $fps - 1) / 4.0) * 4 + 1)
    if ($length -lt 5) { $length = 5 }
    $negative = [string]$JobData.negative
    if (!$negative) { $negative = $negativeDefault }

    Stop-LocalModelForMedia $manage

    Emit 'status' 'Loading Wan 2.2 5B...'
    $comfy = Start-OrReuseComfy $python $comfyDir $comfyOut $comfyIn $logs $comfyUrl $lite 240 $Shared

    $workflow = @{
        '37' = @{ class_type = 'UNETLoader'; inputs = @{ unet_name = $videoModel; weight_dtype = 'default' } }
        '48' = @{ class_type = 'ModelSamplingSD3'; inputs = @{ model = @('37', 0); shift = 8 } }
        '38' = @{ class_type = 'CLIPLoader'; inputs = @{ clip_name = 'umt5_xxl_fp8_e4m3fn_scaled.safetensors'; type = 'wan'; device = 'default' } }
        '39' = @{ class_type = 'VAELoader'; inputs = @{ vae_name = 'wan2.2_vae.safetensors' } }
        '6'  = @{ class_type = 'CLIPTextEncode'; inputs = @{ clip = @('38', 0); text = [string]$JobData.prompt } }
        '7'  = @{ class_type = 'CLIPTextEncode'; inputs = @{ clip = @('38', 0); text = $negative } }
        '55' = @{ class_type = 'Wan22ImageToVideoLatent'; inputs = @{ vae = @('39', 0); width = $width; height = $height; length = $length; batch_size = 1 } }
        '3'  = @{ class_type = 'KSampler'; inputs = @{
                model = @('48', 0); positive = @('6', 0); negative = @('7', 0); latent_image = @('55', 0)
                seed = $seed; steps = $steps; cfg = 5; sampler_name = 'uni_pc'; scheduler = 'simple'; denoise = 1 } }
        # The video VAE overflows 16 GB VRAM when decoding all frames at once; tiled decoding is required.
        '8'  = @{ class_type = 'VAEDecodeTiled'; inputs = @{
                samples = @('3', 0); vae = @('39', 0)
                tile_size = $tile.size; overlap = $tile.overlap
                temporal_size = $tile.temporal; temporal_overlap = $tile.temporalOverlap } }
        '57' = @{ class_type = 'CreateVideo'; inputs = @{ images = @('8', 0); fps = $fps } }
        '58' = @{ class_type = 'SaveVideo'; inputs = @{ video = @('57', 0); filename_prefix = 'mamidlabs-local'; format = 'mp4'; codec = 'h264' } }
    }
    if ($refName) {
        $workflow['56'] = @{ class_type = 'LoadImage'; inputs = @{ image = $refName } }
        $workflow['55'].inputs.start_image = @('56', 0)
    }

    $modeLabel = switch ($mode) {
        'image' { 'image to video' }
        'video'  { 'continue from video' }
        default  { 'text to video' }
    }
    $sn = [math]::Round($length / [double]$fps, 1)
    Emit 'status' "Generating video - $modeLabel ($width x $height, $length frames / $sn s, $steps steps). This can take minutes..."

    $body = @{ prompt = $workflow; client_id = [guid]::NewGuid().ToString() } | ConvertTo-Json -Depth 20 -Compress
    $queued = Invoke-RestMethod "$comfyUrl/prompt" -Method Post -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 60
    $promptId = [string]$queued.prompt_id
    if (!$promptId) { throw 'The ComfyUI request could not be queued.' }

    $produced = $null
    $lastNote = [DateTime]::UtcNow
    $deadline = [DateTime]::UtcNow.AddMinutes(45)
    while ([DateTime]::UtcNow -lt $deadline) {
        if($Shared.Cancel){throw 'Operation stopped.'}
        if ($comfy.HasExited) { throw 'ComfyUI closed during generation (probably not enough VRAM).' }
        Start-Sleep -Milliseconds 1000
        if (([DateTime]::UtcNow - $lastNote).TotalSeconds -ge 30) {
            $lastNote = [DateTime]::UtcNow
            try {
                $progress = Invoke-RestMethod "$comfyUrl/prompt" -TimeoutSec 5
                Emit 'status' "Generating video... ($($progress.exec_info.queue_remaining) jobs in the queue)"
            } catch { }
        }
        $history = $null
        try { $history = Invoke-RestMethod "$comfyUrl/history/$promptId" -TimeoutSec 10 -ErrorAction Stop } catch { continue }
        $entry = $history.$promptId
        if (!$entry) { continue }
        if ($entry.status -and $entry.status.status_str -eq 'error') { throw 'ComfyUI generation error; see the logs\comfyui.stderr.log file.' }
        $saveNode = $entry.outputs.'58'
        if ($saveNode) {
            foreach ($item in @($saveNode.images) + @($saveNode.videos) + @($saveNode.gifs)) {
                if ($item -and $item.filename) { $produced = $item; break }
            }
        }
        if ($produced) { break }
    }
    if (!$produced) { throw 'Video generation timed out.' }

    $sourcePath = Resolve-ComfyOutput ([string]$produced.filename) ([string]$produced.subfolder) $comfyOut @('.mp4', '.webm', '.mkv', '.mov', '.gif')
    $ext = [IO.Path]::GetExtension($produced.filename)
    if (!$ext) { $ext = '.mp4' }
    $finalPath = Join-Path $videoOut ('mamidlabs-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + $ext)
    Copy-Item -LiteralPath $sourcePath -Destination $finalPath -Force
    Remove-Item -LiteralPath $sourcePath -Force -ErrorAction SilentlyContinue

    Emit 'status' 'Unloading the video model from memory...'
    Release-Comfy $comfy $comfyUrl
    $comfy = $null
    $Shared.Process=$null

    # Thumbnail to show in the chat
    $thumb = Join-Path $videoOut ([IO.Path]::GetFileNameWithoutExtension($finalPath) + '.png')
    try { Invoke-MediaTool @{ action = 'video_frame'; path = $finalPath; which = 'first'; out = $thumb; max_side = 720 } | Out-Null } catch { $thumb = $null }

    Emit 'video' $finalPath @{
        prompt = [string]$JobData.prompt; seed = $seed; width = $width; height = $height
        frames = $length; fps = $fps; seconds = $sn; steps = $steps; mode = $mode
        thumbnail = $(if ($thumb -and (Test-Path -LiteralPath $thumb)) { $thumb } else { '' })
    }

    Restore-LocalModelForMedia $manage ([string]$JobData.restoreProfile) $false | Out-Null
    Emit 'status' 'Video ready.'
} catch {
    # If the error happens during startup $comfy is not assigned; the half-open process is in $Shared.
    Stop-Comfy $(if ($comfy) { $comfy } else { $Shared.Process })
    # If the restore attempt ITSELF fails (second parameter $true),
    # the error is swallowed and shown below together with the original error (#16) -
    # showing the user only the video error while hiding that the language model
    # is not ready either would be wrong.
    $restoreFailure = Restore-LocalModelForMedia $manage ([string]$JobData.restoreProfile) $true
    $originalMessage = if ($Shared.Cancel) { 'Operation stopped.' } else { $_.Exception.Message }
    if ($restoreFailure) {
        # If the restore also failed, showing only the original error without telling
        # the user about it would lead them to believe the language model is ready.
        Emit 'error' "$originalMessage In addition, the language model could not be reloaded: $restoreFailure Restart the model manually."
    } elseif ($Shared.Cancel) { Emit 'cancelled' 'Operation stopped.' } else { Emit 'error' $originalMessage }
} finally {
    $Shared.Process=$null
}
