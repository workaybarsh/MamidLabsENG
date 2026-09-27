param([hashtable]$JobData, [hashtable]$Shared)

# Image generation flow:
#   1) The language model is unloaded from VRAM (the model file stays in the Windows file cache)
#   2) ComfyUI + Krea-2 Turbo are loaded and the image is generated
#   3) The image model is closed, the language model is reloaded
#
# Modes:
#   new             : text to image
#   regenerate      : classic img2img, the composition of the source image is kept (denoise = strength)
#   style_reference : Krea-2 style reference LoRA, the source image goes into Qwen3-VL as conditioning
#   identity_edit   : Krea 2 Identity Edit v1.2 (edit the photo by instruction). The source is given both
#                     as a VAE latent (appearance) and to Qwen3-VL (meaning); the face/identity is kept.
#                     Optional second image: scene = image 1, person = image 2.
#                     Template: agent\workflows\krea2_identity_edit.json (ComfyUI UI format).
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
# The stop/restore flow of the language model is shared with videogen-run.ps1 (Amp #17).
. (Join-Path $root 'agent\media-model-swap.ps1')
$comfyDir = Join-Path $root 'ComfyUI'
$python = Join-Path $root 'venv\imagegen\Scripts\python.exe'
$comfyOut = Join-Path $root 'outputs\comfy'
$comfyIn = Join-Path $root 'outputs\input'
$imageOut = Join-Path $root 'outputs\images'
$logs = Join-Path $root 'logs'
$comfyUrl = 'http://127.0.0.1:8188'
$styleLora = Join-Path $comfyDir 'models\loras\krea2_style_reference.safetensors'
$identityLoraName = 'krea2_identity_edit_v1_2.safetensors'
$identityLora = Join-Path $comfyDir ('models\loras\' + $identityLoraName)
$identityNode = Join-Path $comfyDir 'custom_nodes\comfyui-krea2edit\__init__.py'

function Emit([string]$kind, [string]$text, $data = $null) {
    [pscustomobject]@{ kind = $kind; text = $text; usage = $null; data = $data }
}

function Get-Resolution([string]$aspect) {
    switch ($aspect) {
        '16:9' { return @(1344, 768) }
        '9:16' { return @(768, 1344) }
        '4:3'  { return @(1152, 896) }
        '3:4'  { return @(896, 1152) }
        default { return @(1024, 1024) }
    }
}

# Scales the source image to ~1 MP and writes it to the ComfyUI input folder; returns (fileName, width, height).
function Write-SourceImage([string]$path) {
    $img = [System.Drawing.Image]::FromFile($path)
    try {
        $scale = [math]::Sqrt(1048576.0 / ($img.Width * $img.Height))
        $w = [int]([math]::Round($img.Width * $scale / 8.0) * 8)
        $h = [int]([math]::Round($img.Height * $scale / 8.0) * 8)
        if ($w -lt 256) { $w = 256 }
        if ($h -lt 256) { $h = 256 }
        $bmp = New-Object System.Drawing.Bitmap($w, $h)
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        try {
            $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
            $g.DrawImage($img, 0, 0, $w, $h)
        } finally { $g.Dispose() }
        $name = 'ref-' + (Get-Date -Format 'yyyyMMddHHmmss') + '.png'
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

# LoRAs saved with fal's krea-2 training tool use the PEFT prefix (base_model.model.);
# ComfyUI expects diffusion_model. and skips every key that does not match - the LoRA
# is never applied (measured as 528/528 keys "lora key not loaded"). Only the header
# is rewritten; the tensor data and shapes stay the same.
function Repair-LoraKeyPrefix([string]$path) {
    $tmp = $null
    $fs = [IO.File]::OpenRead($path)
    try {
        $reader = New-Object IO.BinaryReader($fs)
        $headerLength = $reader.ReadUInt64()
        if ($headerLength -eq 0 -or $headerLength -gt 100MB) { return }
        $header = [Text.Encoding]::UTF8.GetString($reader.ReadBytes([int]$headerLength))
        if (!$header.Contains('"base_model.model.')) { return }
        $fixed = [Text.Encoding]::UTF8.GetBytes($header.Replace('"base_model.model.', '"diffusion_model.'))
        $pad = (8 - ($fixed.Length % 8)) % 8
        $tmp = "$path.tmp"
        $out = [IO.File]::Create($tmp)
        try {
            $out.Write([BitConverter]::GetBytes([uint64]($fixed.Length + $pad)), 0, 8)
            $out.Write($fixed, 0, $fixed.Length)
            for ($i = 0; $i -lt $pad; $i++) { $out.WriteByte(0x20) }
            $fs.CopyTo($out)
        } finally { $out.Dispose() }
    } finally { $fs.Dispose() }
    Move-Item -LiteralPath $tmp -Destination $path -Force
}

function Resolve-ComfyOutput([string]$filename, [string]$subfolder, [string]$root, [string[]]$extensions) {
    if (!$filename -or $filename.IndexOf([char]0) -ge 0) { throw 'ComfyUI returned an invalid output name.' }
    $relative = if ($subfolder) { Join-Path $subfolder $filename } else { $filename }
    try { $candidate = [IO.Path]::GetFullPath((Join-Path $root $relative)) } catch { throw 'ComfyUI returned an invalid output path.' }
    if (!(Inside $candidate $root)) { throw 'ComfyUI returned a path that escapes the output folder.' }
    if ([IO.Path]::GetExtension($candidate).ToLowerInvariant() -notin $extensions) { throw 'ComfyUI returned an unexpected image file.' }
    if (!(Test-Path -LiteralPath $candidate -PathType Leaf)) { throw 'ComfyUI output file not found.' }
    return $candidate
}

function Inside([string]$path, [string]$folder) {
    $full = [IO.Path]::GetFullPath($path).TrimEnd('\')
    $root = [IO.Path]::GetFullPath($folder).TrimEnd('\')
    return $full.Equals($root, [StringComparison]::OrdinalIgnoreCase) -or
        $full.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)
}

function Resolve-LocalModelName([string]$name, [string]$subfolder = 'models\diffusion_models') {
    if (!$name) { return $null }
    # Lite's GGUF quantized models live under models\unet (.gguf); the full-precision
    # models of MAX/FLASH live under models\diffusion_models (.safetensors).
    $allowedExt = if ($subfolder -eq 'models\unet') { '.gguf' } else { '.safetensors' }
    $modelRoot = Join-Path $comfyDir $subfolder
    try { $candidate = [IO.Path]::GetFullPath((Join-Path $modelRoot $name)) } catch { throw 'The local image model path is invalid.' }
    if (!(Inside $candidate $modelRoot) -or [IO.Path]::GetExtension($candidate).ToLowerInvariant() -ne $allowedExt) { throw "The local image model can only be a $allowedExt file under $subfolder." }
    if (!(Test-Path -LiteralPath $candidate -PathType Leaf)) {
        # An external file added without copying (registered with ComfyUI via settings\comfy-extra-models.yaml).
        $external = Resolve-ExternalMediaModel $root $name (Split-Path -Leaf $subfolder)
        if ($external) { return $external }
        throw 'The selected local image model was not found.'
    }
    return $candidate.Substring($modelRoot.Length + 1).Replace('\', '/')
}

# Identity edit resolution: the aspect ratio of the source is kept, scaled to ~1 MP and
# rounded to a multiple of 16 (VAE /8, DiT patch size /2). Template note: 1 MP is the best
# balance; do not exceed ~1.5 MP for images with two people.
function Get-EditResolution([int]$sourceWidth, [int]$sourceHeight) {
    $scale = [math]::Sqrt(1048576.0 / ([double]$sourceWidth * $sourceHeight))
    $w = [int]([math]::Round($sourceWidth * $scale / 16.0) * 16)
    $h = [int]([math]::Round($sourceHeight * $scale / 16.0) * 16)
    return @([math]::Max(512, $w), [math]::Max(512, $h))
}

# The API-format equivalent of agent\workflows\krea2_identity_edit.json. The only difference
# from the UI template: target_latent is connected (node pack README: the source is encoded
# before sampling starts, so the model is not split on cards with tight VRAM).
function New-IdentityEditWorkflow([hashtable]$wf, [string]$prompt, [int]$width, [int]$height, [long]$seed, [int]$steps,
                                  [string]$refName, [string]$refNameB, [double]$refBoost, [int]$groundingPx) {
    $wf['71'] = @{ class_type = 'LoraLoaderModelOnly'; inputs = @{ model = @('10', 0); lora_name = $identityLoraName; strength_model = 1.0 } }
    $wf['72'] = @{ class_type = 'LoadImage'; inputs = @{ image = $refName } }
    $wf['73'] = @{ class_type = 'VAEEncode'; inputs = @{ pixels = @('72', 0); vae = @('12', 0) } }
    $wf['82'] = @{ class_type = 'EmptySD3LatentImage'; inputs = @{ width = $width; height = $height; batch_size = 1 } }
    $patch = @{ model = @('71', 0); source_latent = @('73', 0); vae = @('12', 0); source_image = @('72', 0)
                target_latent = @('82', 0); ref_boost = $refBoost; ref_boost_a = 1.0; fit_mode = 'fit' }
    $positive = @{ clip = @('11', 0); prompt = $prompt; image = @('72', 0); grounding_px = $groundingPx; system_prompt = '' }
    # Negative: empty instruction + the same image (the unconditional case in training); has no effect at CFG 1.
    $negative = @{ clip = @('11', 0); prompt = ''; image = @('72', 0); grounding_px = $groundingPx; system_prompt = '' }
    if ($refNameB) {
        # Two images: scene is source 1, person is source 2. ref_boost applies to the last source (the person).
        $wf['90'] = @{ class_type = 'LoadImage'; inputs = @{ image = $refNameB } }
        $wf['92'] = @{ class_type = 'VAEEncode'; inputs = @{ pixels = @('90', 0); vae = @('12', 0) } }
        $patch.source_latent_b = @('92', 0); $patch.source_image_b = @('90', 0)
        $positive.image_b = @('90', 0); $negative.image_b = @('90', 0)
    }
    $wf['79'] = @{ class_type = 'Krea2EditModelPatch'; inputs = $patch }
    $wf['84'] = @{ class_type = 'Krea2EditGroundedEncode'; inputs = $positive }
    $wf['85'] = @{ class_type = 'Krea2EditGroundedEncode'; inputs = $negative }
    $wf['3'] = @{ class_type = 'KSampler'; inputs = @{
        model = @('79', 0); positive = @('84', 0); negative = @('85', 0); latent_image = @('82', 0)
        seed = $seed; steps = $steps; cfg = 1; sampler_name = 'euler'; scheduler = 'simple'; denoise = 1.0 } }
    return $wf
}

function New-Workflow([string]$mode, [string]$prompt, [int]$width, [int]$height, [long]$seed, [int]$steps, [string]$refName, [double]$strength, [bool]$realism, [double]$realismStrength, [bool]$lite, [string]$refNameB = '', [double]$refBoost = 4.0, [int]$groundingPx = 768) {
    # Lite (8 GB VRAM): the krea2_turbo_fp8_scaled.safetensors file of MAX/FLASH (~13.1 GB)
    # does not fit the card on its own. Instead a GGUF-quantized version (Q4_K_M, ~7.49 GB)
    # is loaded with the 'UnetLoaderGGUF' node (requires the ComfyUI-GGUF custom node);
    # the text encoder/VAE/LoRAs stay the same as MAX/FLASH -
    # only the diffusion model loader and its file change.
    $unetNode = if ($lite) {
        @{ class_type = 'UnetLoaderGGUF'; inputs = @{ unet_name = $(if($JobData.localModel){[string]$JobData.localModel}else{'krea2_turbo-Q4_K_M.gguf'}) } }
    } else {
        @{ class_type = 'UNETLoader'; inputs = @{ unet_name = $(if($JobData.localModel){[string]$JobData.localModel}else{'krea2_turbo_fp8_scaled.safetensors'}); weight_dtype = 'default' } }
    }
    $wf = @{
        '10' = $unetNode
        '11' = @{ class_type = 'CLIPLoader'; inputs = @{ clip_name = 'qwen3vl_4b_fp8_scaled.safetensors'; type = 'krea2'; device = 'default' } }
        '12' = @{ class_type = 'VAELoader'; inputs = @{ vae_name = 'qwen_image_vae.safetensors' } }
        '8'  = @{ class_type = 'VAEDecode'; inputs = @{ samples = @('3', 0); vae = @('12', 0) } }
        '9'  = @{ class_type = 'SaveImage'; inputs = @{ images = @('8', 0); filename_prefix = 'mamidlabs-local' } }
    }

    if ($mode -eq 'identity_edit') {
        $wf['9'].inputs.filename_prefix = 'mamidlabs-edit'
        return (New-IdentityEditWorkflow $wf $prompt $width $height $seed $steps $refName $refNameB $refBoost $groundingPx)
    }

    # Realistic light/shadow/reflection LoRA; attached at the start of the model chain.
    $modelSource = @('10', 0)
    if ($realism) {
        $wf['70'] = @{ class_type = 'LoraLoaderModelOnly'; inputs = @{ model = @('10', 0); lora_name = 'krea2_realism_lora.safetensors'; strength_model = $realismStrength } }
        $modelSource = @('70', 0)
    }

    if ($mode -eq 'style_reference') {
        # Template: Image Style Reference (Krea-2 Turbo)
        $wf['15'] = @{ class_type = 'LoraLoaderModelOnly'; inputs = @{ model = $modelSource; lora_name = 'krea2_style_reference.safetensors'; strength_model = 1.0 } }
        $wf['64'] = @{ class_type = 'ModelSamplingFlux'; inputs = @{ model = @('15', 0); max_shift = 1.15; base_shift = 0.5; width = $width; height = $height } }
        $wf['20'] = @{ class_type = 'LoadImage'; inputs = @{ image = $refName } }
        $wf['52'] = @{ class_type = 'TextEncodeQwenImageEditPlus'; inputs = @{ clip = @('11', 0); prompt = $prompt; vae = @('12', 0); image1 = @('20', 0) } }
        $wf['53'] = @{ class_type = 'FluxKontextMultiReferenceLatentMethod'; inputs = @{ conditioning = @('52', 0); reference_latents_method = 'index_timestep_zero' } }
        $wf['13'] = @{ class_type = 'ConditioningZeroOut'; inputs = @{ conditioning = @('52', 0) } }
        $wf['57'] = @{ class_type = 'CFGGuider'; inputs = @{ model = @('64', 0); positive = @('53', 0); negative = @('13', 0); cfg = 1.0 } }
        $wf['59'] = @{ class_type = 'KSamplerSelect'; inputs = @{ sampler_name = 'euler' } }
        $wf['60'] = @{ class_type = 'BasicScheduler'; inputs = @{ model = @('64', 0); scheduler = 'simple'; steps = $steps; denoise = 1.0 } }
        $wf['63'] = @{ class_type = 'RandomNoise'; inputs = @{ noise_seed = $seed } }
        $wf['61'] = @{ class_type = 'EmptyLatentImage'; inputs = @{ width = $width; height = $height; batch_size = 1 } }
        $wf['3']  = @{ class_type = 'SamplerCustomAdvanced'; inputs = @{ noise = @('63', 0); guider = @('57', 0); sampler = @('59', 0); sigmas = @('60', 0); latent_image = @('61', 0) } }
        return $wf
    }

    $wf['6'] = @{ class_type = 'CLIPTextEncode'; inputs = @{ clip = @('11', 0); text = $prompt } }
    $wf['13'] = @{ class_type = 'ConditioningZeroOut'; inputs = @{ conditioning = @('6', 0) } }
    if ($mode -eq 'regenerate') {
        $wf['20'] = @{ class_type = 'LoadImage'; inputs = @{ image = $refName } }
        $wf['21'] = @{ class_type = 'VAEEncode'; inputs = @{ pixels = @('20', 0); vae = @('12', 0) } }
        $latent = @('21', 0)
        $denoise = $strength
    } else {
        $wf['5'] = @{ class_type = 'EmptyLatentImage'; inputs = @{ width = $width; height = $height; batch_size = 1 } }
        $latent = @('5', 0)
        $denoise = 1.0
    }
    $wf['3'] = @{ class_type = 'KSampler'; inputs = @{
        model = $modelSource; positive = @('6', 0); negative = @('13', 0); latent_image = $latent
        seed = $seed; steps = $steps; cfg = 1; sampler_name = 'euler'; scheduler = 'simple'; denoise = $denoise } }
    return $wf
}

$comfy = $null
try {
    foreach ($dir in @($comfyOut, $comfyIn, $imageOut, $logs)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    if (!(Test-Path -LiteralPath $python -PathType Leaf)) { throw "Image generation environment not found: $python" }
    $lite = [bool]$JobData.lite
    # Krea-2's separate UNET lives under diffusion_models; Lite's GGUF quantized version
    # lives under models\unet, where the ComfyUI-GGUF custom node looks for it.
    $modelFolder = if ($lite) { 'models\unet' } else { 'models\diffusion_models' }
    if ($JobData.localModel) { $JobData.localModel = Resolve-LocalModelName ([string]$JobData.localModel) $modelFolder }

    $mode = [string]$JobData.mode
    if (!$mode) { $mode = 'new' }
    $source = [string]$JobData.sourceImage
    if ($mode -ne 'new' -and (!$source -or !(Test-Path -LiteralPath $source -PathType Leaf))) {
        throw 'This mode needs a source image; attach an image to the chat first.'
    }
    if ($mode -eq 'style_reference' -and !(Test-Path -LiteralPath $styleLora -PathType Leaf)) {
        throw 'Style reference LoRA file not found: krea2_style_reference.safetensors'
    }
    $sourceB = [string]$JobData.sourceImageB
    if ($mode -eq 'identity_edit') {
        # Not offered on LITE (8 GB VRAM) (qwen-inference.ps1 removes it from the tool's enum); if
        # the model still asks for it with an old/cached tool call it is explicitly rejected here. Reason:
        # the GGUF diffusion model + the text/image encoder do not fit in VRAM together and it goes up
        # to 10-15 min (see the hardware table in AGENTS.md; a small r64/r128 LoRA does not fix it either).
        if ($lite) { throw 'Photo editing (identity-preserving) is not supported on the LITE profile; it is extremely slow on an 8 GB VRAM card. Use the regenerate mode.' }
        if (!(Test-Path -LiteralPath $identityNode -PathType Leaf)) {
            throw 'The comfyui-krea2edit node pack for photo editing is not installed. Update the installation: install-mamidlabs.ps1 -Stage comfyui'
        }
        if (!(Test-Path -LiteralPath $identityLora -PathType Leaf)) {
            throw "The photo editing LoRA was not found: $identityLoraName. Profiles -> Profile manager -> Download missing."
        }
        if ($sourceB -and !(Test-Path -LiteralPath $sourceB -PathType Leaf)) { $sourceB = '' }
    } else { $sourceB = '' }

    $size = Get-Resolution ([string]$JobData.aspect)
    $width = $size[0]; $height = $size[1]
    $refName = $null
    $refNameB = ''
    if ($mode -ne 'new') {
        $written = Write-SourceImage $source
        $refName = $written[0]
        if ($mode -eq 'regenerate') { $width = $written[1]; $height = $written[2] }
        if ($mode -eq 'identity_edit') {
            # The output follows the aspect ratio of the scene (image 1); fit_mode='fit' places the person into it.
            $size = Get-EditResolution $written[1] $written[2]
            $width = $size[0]; $height = $size[1]
            if ($sourceB) { $refNameB = (Write-SourceImage $sourceB)[0] }
        }
    }

    $seed = if ($JobData.seed) { [long]$JobData.seed } else { [long](Get-Random -Minimum 1 -Maximum 2147483646) }
    # The Turbo + Identity Edit template finds 10 steps balanced (8 = more faithful to the instruction, 12 = more facial detail).
    $steps = if ($JobData.steps) { [int]$JobData.steps } elseif ($mode -eq 'identity_edit') { 10 } else { 8 }
    # Fidelity setting (ref_boost): 4 recommended, >10 breaks removal/replacement edits.
    $refBoost = if ($JobData.refBoost) { [math]::Min(10.0, [math]::Max(0.5, [double]$JobData.refBoost)) } else { 4.0 }
    $strength = if ($JobData.strength) { [double]$JobData.strength } else { 0.6 }
    if ($strength -lt 0.2) { $strength = 0.2 }
    if ($strength -gt 0.95) { $strength = 0.95 }

    Stop-LocalModelForMedia $manage

    Emit 'status' $(if ($lite) { 'Loading Krea-2 Turbo (GGUF, low-VRAM mode)...' } else { 'Loading Krea-2 Turbo...' })
    $comfy = Start-OrReuseComfy $python $comfyDir $comfyOut $comfyIn $logs $comfyUrl $lite 180 $Shared

    # Comes in on only when the user explicitly asks for a realistic image (qwen-inference.ps1 Test-RealismIntent).
    $realism = [bool]$JobData.realism
    # In identity editing the realism LoRA is not added to the chain; the source photo is already realistic.
    if ($mode -eq 'identity_edit') { $realism = $false }
    $realismStrength = if ($JobData.realismStrength) { [double]$JobData.realismStrength } else { 0.8 }
    $realismLora = Join-Path $comfyDir 'models\loras\krea2_realism_lora.safetensors'
    if (!(Test-Path -LiteralPath $realismLora -PathType Leaf)) { $realism = $false }
    if ($realism) {
        try { Repair-LoraKeyPrefix $realismLora }
        catch { Emit 'status' ("The realism LoRA could not be converted to the ComfyUI format: " + $_.Exception.Message) }
    }

    $modeLabel = switch ($mode) {
        'regenerate' { "regenerate from image (strength $strength)" }
        'style_reference' { 'style reference' }
        'identity_edit' { if ($refNameB) { 'photo editing (scene + person)' } else { 'photo editing (identity preserved)' } }
        default { 'text to image' }
    }
    if ($realism) { $modeLabel += ' + realism' }
    Emit 'status' "Generating image - $modeLabel ($width x $height, $steps steps)..."

    $workflow = New-Workflow $mode $JobData.prompt $width $height $seed $steps $refName $strength $realism $realismStrength $lite $refNameB $refBoost
    $body = @{ prompt = $workflow; client_id = [guid]::NewGuid().ToString() } | ConvertTo-Json -Depth 20 -Compress
    $queued = Invoke-RestMethod "$comfyUrl/prompt" -Method Post -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 60
    $promptId = [string]$queued.prompt_id
    if (!$promptId) { throw 'The ComfyUI request could not be queued.' }

    $produced = $null
    $deadline = [DateTime]::UtcNow.AddSeconds(900)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($Shared.Cancel) { throw 'Operation stopped.' }
        if ($comfy.HasExited) { throw 'ComfyUI closed during generation (probably not enough VRAM).' }
        Start-Sleep -Milliseconds 800
        $history = $null
        try { $history = Invoke-RestMethod "$comfyUrl/history/$promptId" -TimeoutSec 10 -ErrorAction Stop } catch { continue }
        $entry = $history.$promptId
        if (!$entry) { continue }
        if ($entry.status -and $entry.status.status_str -eq 'error') { throw 'ComfyUI generation error; see the logs\comfyui.stderr.log file.' }
        foreach ($nodeOutput in $entry.outputs.PSObject.Properties) {
            foreach ($image in @($nodeOutput.Value.images)) {
                if ($image.filename) { $produced = $image; break }
            }
            if ($produced) { break }
        }
        if ($produced) { break }
    }
    if (!$produced) { throw 'Image generation timed out.' }

    $sourcePath = Resolve-ComfyOutput ([string]$produced.filename) ([string]$produced.subfolder) $comfyOut @('.png', '.jpg', '.jpeg', '.webp')
    $finalPath = Join-Path $imageOut ('mamidlabs-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.png')
    Copy-Item -LiteralPath $sourcePath -Destination $finalPath -Force
    # Keep intermediate files from piling up: after the ComfyUI output is copied, keep the last 20 references.
    Remove-Item -LiteralPath $sourcePath -Force -ErrorAction SilentlyContinue
    Get-ChildItem -LiteralPath $comfyIn -Filter 'ref-*.png' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -Skip 20 |
        Remove-Item -Force -ErrorAction SilentlyContinue

    Emit 'status' 'Unloading the image model from memory...'
    Release-Comfy $comfy $comfyUrl
    $comfy = $null
    $Shared.Process = $null

    Emit 'image' $finalPath @{ prompt = [string]$JobData.prompt; seed = $seed; width = $width; height = $height; steps = $steps; mode = $mode }

    Restore-LocalModelForMedia $manage ([string]$JobData.restoreProfile) $false | Out-Null
    Emit 'done' 'Image ready.'
} catch {
    # If the error happens during startup $comfy is not assigned; the half-open process is in $Shared.
    Stop-Comfy $(if ($comfy) { $comfy } else { $Shared.Process })
    # If the restore attempt ITSELF fails (second parameter $true),
    # the error is swallowed and shown below together with the original error (#16) -
    # showing the user only the image error while hiding that the language model
    # is not ready either would be wrong.
    $restoreFailure = Restore-LocalModelForMedia $manage ([string]$JobData.restoreProfile) $true
    $originalMessage = if ($Shared.Cancel) { 'Operation stopped.' } else { $_.Exception.Message }
    if ($restoreFailure) {
        # If the restore also failed, showing only the original error without telling
        # the user about it would lead them to believe the language model is ready.
        Emit 'error' "$originalMessage In addition, the language model could not be reloaded: $restoreFailure Restart the model manually."
    } elseif ($Shared.Cancel) { Emit 'cancelled' 'Operation stopped.' } else { Emit 'error' $originalMessage }
} finally {
    $Shared.Process = $null
}
