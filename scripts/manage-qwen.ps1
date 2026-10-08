param(
    [ValidateSet('start', 'stop', 'switch')][string]$Action = 'switch',
    [ValidateSet('max', 'flash', 'lite')][string]$Profile = 'max',
    [string]$ModelId,
    # Skip the memory check (for troubleshooting). Not needed in normal use.
    [switch]$SkipMemoryCheck
)

$ErrorActionPreference = 'Stop'
# Derive the install root from this file's own location: this file always lives under <root>\scripts\.
$root = Split-Path -Parent $PSScriptRoot
$server = Join-Path $root 'llama.cpp\b10998\llama-server.exe'
# Alias per profile. MAX and FLASH share the same 27B file; Lite loads a separate 9B model
# for 8 GB VRAM / 16 GB RAM machines. This table must stay identical to
# $script:localProfileAliases in model-registry.ps1.
$profileAliases = @{ max = 'qwen3.8-27b-max'; flash = 'qwen3.8-27b-flash'; lite = 'qwen3.5-9b-lite' }
$liteDir = Join-Path $root 'models\Huihui-Qwen3.5-9B-abliterated-i1-GGUF'
$logs = Join-Path $root 'logs'
. (Join-Path $root 'agent\process-launch.ps1')

# The settings are needed for the profile choice, so they are read before the model path is determined.
$settings = $null
$settingsPath = Join-Path $root 'settings\api.json'
if ($Action -ne 'stop' -and (Test-Path -LiteralPath $settingsPath)) {
    $settings = Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json
}

if ($Profile -eq 'lite') {
    # The default is Q5_K_M (~6.6 GB). Measured on a real machine (RTX 4060 Laptop, 8 GB)
    # with 32K context: it uses 7500 MiB VRAM, ~450-500 MiB stays free -
    # tight but a margin the user accepted (the other MAX/FLASH profiles run with
    # similar margins on other machines). llama.cpp's own automatic fit check marks
    # this as "does not fully fit", but since --gpu-layers 99 is forced manually it
    # loads anyway; performance does not drop (measured ~35 tok/s, only ~13% slower
    # than Q4). For a more comfortable margin you can go back to Q4_K_M (~5.7 GB) with
    # the liteModel field in settings/api.json; Q6_K (~7.4 GB) never fits on an 8 GB card
    # (the file itself exceeds the free VRAM).
    $liteFile = 'Huihui-Qwen3.5-9B-abliterated.i1-Q5_K_M.gguf'
    $liteCtx = '32768'
    # Thinking stays on; this only limits how many tokens are spent thinking.
    # -1 unlimited, 0 ends thinking immediately, N>0 token budget.
    #
    # Why it is needed: the Qwen3.5 template ignores --reasoning-effort, so effort can
    # only be tuned with this server-enforced budget. On an RTX 4060 the same simple
    # request took 1451-2391 tokens and ~50 s unlimited; with a 300-token budget it took
    # 408-429 tokens and ~10 s, and the answer quality was preserved.
    $liteThinkBudget = '300'
    if ($settings -and $settings.liteModel) { $liteFile = [string]$settings.liteModel }
    if ($settings -and $settings.liteContext) { $liteCtx = [string][int]$settings.liteContext }
    if ($settings -and $null -ne $settings.liteReasoningBudget) { $liteThinkBudget = [string][int]$settings.liteReasoningBudget }
    $model = Join-Path $liteDir $liteFile
    # Lite's image projector (mmproj Q8_0) is downloaded as a separate file from the catalog;
    # if it is not in the folder Lite runs text-only and images are read only via OCR.
    $projector = @(Get-ChildItem -LiteralPath $liteDir -Filter '*mmproj*.gguf' -File -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName)[0]
} else {
    $model = Join-Path $root 'models\Qwen3.8-27B-Abliterated-IQ4_XS\Qwen3.8-27B-Abliterated-IQ4_XS.gguf'
    $projector = Join-Path $root 'models\Qwen3.8-27B-Abliterated-IQ4_XS\mmproj-Qwen3.8-27B-Abliterated-F16.gguf'
}
$desiredAlias = $profileAliases[$Profile]
$custom=$null
if($settings){
    # UI switches carry a snapshot of the choice; OCR/media restore callers use saved settings.
    $selectedId = if ($ModelId) { $ModelId } else { [string]$settings.selectedLocalModel }
    if($selectedId -and $selectedId -notin @('builtin-max', 'builtin-flash', 'builtin-lite')){
        $custom=@($settings.localModels | Where-Object {$_.id -eq $selectedId})[0]
        if(!$custom){throw 'The registered local model was not found.'}
        $model=[string]$custom.path;$desiredAlias=[string]$custom.id
        $projector=@(Get-ChildItem -LiteralPath (Split-Path $model) -Filter '*mmproj*.gguf' -File | Select-Object -First 1 -ExpandProperty FullName)[0]
    }
}
$gate = [System.Threading.Mutex]::new($false, 'Local\QwenLocalModelControl')
$ownsGate = $false

# Memory gate: decides whether to start based on the estimated need + measured free VRAM +
# a safety margin. If another application holds the GPU, llama-server is not started;
# otherwise the layers would spill into Windows' shared memory and slow the chat down many
# times over. The user is told the reason explicitly.
function Test-MemoryBudget([string]$modelPath, [string]$profileName, [string]$projectorPath) {
    $smi = (Get-Command nvidia-smi -ErrorAction SilentlyContinue).Source
    if (!$smi) { return }  # Without NVIDIA drivers the check is meaningless; the real error comes from llama-server.
    $freeMiB = 0
    try {
        $line = (& $smi --query-gpu=memory.free --format=csv,noheader,nounits | Select-Object -First 1)
        $freeMiB = [int]($line -replace '\D', '')
    } catch { return }  # If nvidia-smi gives unexpected output skip the check, do not block the installation.
    if ($freeMiB -le 0) { return }
    $modelMiB = [math]::Ceiling((Get-Item -LiteralPath $modelPath).Length / 1MB)
    # On Lite the image projector is loaded into VRAM together with the model; on MAX/FLASH
    # it runs on the CPU with --no-mmproj-offload and does not count toward the VRAM budget.
    if ($profileName -eq 'lite' -and $projectorPath -and (Test-Path -LiteralPath $projectorPath -PathType Leaf)) {
        $modelMiB += [math]::Ceiling((Get-Item -LiteralPath $projectorPath).Length / 1MB)
    }
    # Real measurement (Lite, Q5_K_M, 32K context, RTX 4060 Laptop, default profile):
    # when the 6220 MiB file was loaded total VRAM use reached ~7500 MiB,
    # leaving ~450-500 MiB free on the card - tight but a margin the user explicitly
    # accepted (MAX/FLASH also run with similar margins on other machines). So the
    # margin for Lite was kept small: a much wider margin would, under the same ambient
    # conditions (browser + desktop holding ~1.5 GB VRAM), silently reject the default
    # profile and deprive the user of a working installation.
    #
    # MAX/FLASH real measurement (RTX 5060 Ti 16 GB, 32K context, llama.cpp --verbose
    # memory dump): 644 MiB (token_embd) of the 14600 MiB file stays on the CPU;
    # on the GPU 13729 weights + 576 KV + 150 recurrent + 184 compute = ~14640 MiB, i.e.
    # file size + ~40 MiB. The margin was kept at 300 MiB. The previous +2560 MiB margin asked
    # for 17160+ MiB; since that is larger than the free space of a 16 GB card (~15000-15700 MiB)
    # MAX could never start on its target hardware.
    $overheadMiB = 300
    $neededMiB = $modelMiB + $overheadMiB
    if ($freeMiB -lt $neededMiB) {
        throw ("Not enough free VRAM: $freeMiB MiB free, the `"$profileName`" profile " +
            "needs an estimated $neededMiB MiB at least (model ~$modelMiB MiB, cache/compute " +
            "margin $overheadMiB MiB). Close other applications using the GPU (browser hardware " +
            "acceleration, another MamidLabs process etc.) and try again, or " +
            "choose a smaller profile.")
    }
}

# A ComfyUI left open between jobs (see agent\media-model-swap.ps1) holds ~300 MiB VRAM for
# its CUDA context even with the models unloaded (measured on an RTX 5060 Ti).
# On the 8 GB Lite card this margin may keep the language model from fitting. If memory is
# short and ComfyUI's queue is empty it is closed; the next image/video reopens it (~15-20 s).
# If a job is running it is not touched.
function Stop-IdleComfy {
    $comfy = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*ComfyUI\main.py*' } | Select-Object -First 1
    if (!$comfy) { return $false }
    try {
        $queue = Invoke-RestMethod 'http://127.0.0.1:8188/queue' -TimeoutSec 3 -ErrorAction Stop
        if (@($queue.queue_running).Count -gt 0 -or @($queue.queue_pending).Count -gt 0) { return $false }
    } catch { }  # A process that does not respond is unusable anyway; it can be closed.
    try {
        $process = [Diagnostics.Process]::GetProcessById([int]$comfy.ProcessId)
        $process.Kill($true)
        $process.WaitForExit(20000) | Out-Null
    } catch { return $false }
    Start-Sleep -Milliseconds 1500  # The driver frees VRAM with a slight delay.
    return $true
}

function Get-ManagedServers {
    @(Get-CimInstance Win32_Process -Filter "Name='llama-server.exe'" -ErrorAction SilentlyContinue | Where-Object {
        $_.ExecutablePath -and
        $_.ExecutablePath.Equals($server, [System.StringComparison]::OrdinalIgnoreCase) -and
        $_.CommandLine -match '--port\s+8080(?:\s|$)'
    })
}

try {
    try { $ownsGate = $gate.WaitOne(30000) }
    catch [System.Threading.AbandonedMutexException] { $ownsGate = $true }
    if (!$ownsGate) { throw 'Model control is being used by another process.' }

    $running = @(Get-ManagedServers)
    if ($Action -ne 'stop' -and $running.Count -eq 1 -and $running[0].CommandLine -match ('--alias\s+' + [regex]::Escape($desiredAlias) + '(?:\s|$)')) {
        "Already loaded: $desiredAlias (PID $($running[0].ProcessId))"
        return
    }

    foreach ($serverProcess in $running) {
        Stop-Process -Id $serverProcess.ProcessId -Force -ErrorAction SilentlyContinue
    }
    if ($running.Count -gt 0) {
        $deadline = [DateTime]::UtcNow.AddSeconds(20)
        while (@(Get-ManagedServers).Count -gt 0 -and [DateTime]::UtcNow -lt $deadline) {
            Start-Sleep -Milliseconds 250
        }
        if (@(Get-ManagedServers).Count -gt 0) { throw 'The previous model process did not exit.' }
        Start-Sleep -Milliseconds 500
    }

    if ($Action -eq 'stop') { 'Model stopped.'; return }

    if (!(Test-Path -LiteralPath $server -PathType Leaf)) { throw "llama-server not found: $server" }
    if (!(Test-Path -LiteralPath $model -PathType Leaf)) { throw "Model not found: $model" }
    # The Lite profile also runs without an image projector; on the others mmproj is mandatory.
    if (!$custom -and $Profile -ne 'lite' -and !(Test-Path -LiteralPath $projector -PathType Leaf)) { throw "Image projector not found: $projector" }
    # The previous process was already stopped above; free VRAM is measured here so the
    # memory freed by the old process is counted too.
    if (!$SkipMemoryCheck) {
        try { Test-MemoryBudget $model $Profile $projector }
        catch {
            # If no room can be made, the first (explanatory) error is passed on as is.
            if (!(Stop-IdleComfy)) { throw }
            Test-MemoryBudget $model $Profile $projector
        }
    }
    New-Item -ItemType Directory -Path $logs -Force | Out-Null

    $predict = switch ($Profile) { 'lite' { '3072' } 'flash' { '4096' } default { '8192' } }
    # The model names are max/flash/lite; the corresponding reasoning values for the
    # llama.cpp template are still sent as low/xhigh.
    $reasoningEffort = if ($Profile -eq 'max') { 'xhigh' } else { 'low' }
    if ($Profile -eq 'lite') {
        # 8 GB VRAM budget: ~5.7 GB weights + q4_0 KV cache + compute buffer.
        # Since the Windows desktop also holds 0.5-1 GB VRAM the default is 32K context
        # ($liteCtx above, overridable via settings.liteContext); the peak VRAM is pulled
        # down further by shrinking the batch.
        $serverArgs = @(
            '--model', $model,
            '--alias', $desiredAlias, '--host', '127.0.0.1', '--port', '8080',
            '--ctx-size', $liteCtx, '--predict', $predict, '--parallel', '1',
            '--gpu-layers', '99', '--flash-attn', 'on', '--cache-type-k', 'q4_0',
            '--cache-type-v', 'q4_0', '--cache-reuse', '256', '--batch-size', '256', '--ubatch-size', '128',
            '--threads', '4', '--threads-batch', '8', '--jinja', '--reasoning', 'on',
            '--reasoning-effort', $reasoningEffort, '--reasoning-format', 'deepseek',
            '--reasoning-budget', $liteThinkBudget,
            '--metrics', '--no-context-shift'
        )
        # The projector is added only if it is in the folder; otherwise Lite runs text-only.
        if ($projector) { $serverArgs += @('--mmproj', $projector, '--image-min-tokens', '512') }
    }
    else {
        # The image projector runs on the CPU (--no-mmproj-offload). mmproj F16 on the GPU needs
        # 1133 MiB with weights + compute buffer; added to the language model's ~14640 MiB it exceeds
        # the free space of a 16 GB card, spills ~420 MiB into shared memory and slowed
        # everything down. RTX 5060 Ti measurement, GPU -> CPU: chat 19.6 -> 24.8 tok/s,
        # single image reading 56-58 s -> 30 s.
        $serverArgs = @(
            '--model', $model, '--mmproj', $projector, '--no-mmproj-offload', '--image-min-tokens', '1024',
            '--alias', $desiredAlias, '--host', '127.0.0.1', '--port', '8080',
            '--ctx-size', '32768', '--predict', $predict, '--parallel', '1',
            '--gpu-layers', '99', '--flash-attn', 'on', '--cache-type-k', 'q4_0',
            '--cache-type-v', 'q4_0', '--cache-reuse', '256', '--batch-size', '512', '--ubatch-size', '256',
            '--threads', '6', '--threads-batch', '12', '--jinja', '--reasoning', 'on',
            '--reasoning-effort', $reasoningEffort, '--reasoning-format', 'deepseek',
            '--metrics', '--no-context-shift'
        )
    }
    if($custom){
        $serverArgs=@('--model',$model,'--alias',$desiredAlias,'--host','127.0.0.1','--port','8080','--ctx-size','8192','--predict','4096','--parallel','1','--gpu-layers','99','--flash-attn','on','--cache-reuse','256','--jinja','--metrics')
        if($projector){$serverArgs+=@('--mmproj',$projector)}
    }
    # Does the GGUF header contain an MTP/nextn block? llama.cpp's `--spec-type draft-mtp`
    # only works on models that carry MTP layers; passing the flag to one that does not
    # makes the server fail ("model doesn't contain MTP layers"). Metadata and tensor names
    # sit as plain text in the header; the MTP block is named "nextn" (e.g.
    # blk.32.nextn.eh_proj.weight).
    function Test-GgufHasMtp([string]$Path) {
        try {
            $fs = [IO.File]::OpenRead($Path)
            try {
                $buf = [byte[]]::new(16777216)
                $n = $fs.Read($buf, 0, $buf.Length)
            }
            finally { $fs.Dispose() }
            return ([Text.Encoding]::ASCII.GetString($buf, 0, $n)).Contains('nextn')
        }
        catch { return $false }
    }
    # MTP (Multi-Token Prediction) speculative decoding. The MAX/FLASH 27B GGUF and the LITE
    # 9B GGUF (added automatically on download; see agent/mtp_inject.py) carry a BUILT-IN
    # MTP/nextn draft head (blk.<N>.nextn.*, nextn_predict_layers=1), so NO separate draft
    # model is needed - the head inside the same file is used. Output is lossless (accepted
    # tokens are verified against the target model) and it gives ~2x speed (acceptance ~0.93
    # measured on LITE; 0.35-0.53 on Q4_K_M per the 27B model card). The flag is added only
    # if the model really carries MTP layers. The same check applies to custom models.
    # Disable with settings/api.json -> "mtp": false if a compatibility issue shows up.
    $mtpEnabled = $true
    if ($settings -and $null -ne $settings.mtp) { $mtpEnabled = [bool]$settings.mtp }
    if (!$custom -and $mtpEnabled -and (Test-GgufHasMtp $model)) {
        $serverArgs += @('--spec-type', 'draft-mtp')
    }
    # Start-Process joins ArgumentList on Windows; quote paths explicitly.
    $serverArgs=@($serverArgs | ForEach-Object {if($_ -match '\s'){'"'+$_+'"'}else{$_}})
    # Start without inheriting the caller's stdout pipe; otherwise "manage-qwen | ..." never finishes.
    $child = Start-DetachedProcess $server $serverArgs (Split-Path $server) `
        (Join-Path $logs "qwen-$Profile.stdout.log") (Join-Path $logs "qwen-$Profile.stderr.log")
    if (!$child) { throw 'The model process could not be started.' }
    "Loading: $desiredAlias (PID $($child.Id))"
}
finally {
    if ($ownsGate) { $gate.ReleaseMutex() | Out-Null }
    $gate.Dispose()
}
