param(
    [ValidateSet('start', 'stop', 'switch', 'closecomfy')][string]$Action = 'switch',
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
if ($Action -notin @('stop', 'closecomfy') -and (Test-Path -LiteralPath $settingsPath)) {
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
    # MAX/FLASH real measurement (RTX 5060 Ti 16 GB, 32K context, MTP on, llama.cpp):
    # on a clean GPU (ComfyUI closed) the 14600 MiB file takes ~15170-15650 MiB on the
    # GPU; llama.cpp keeps the big embedding tensor on the CPU when memory is short and
    # absorbs small differences that way. With 15244 MiB free the model loaded and ran at
    # ~45 tok/s (measured). A 1000 MiB margin (threshold 15600) and an 800 MiB margin
    # (threshold 15400) wrongly rejected that state on a real machine: the
    # "Model operation failed" error. So the margin is 600 MiB for the 16 GB profiles
    # (threshold 15200); a clean GPU plus reasonable desktop usage lets the model load.
    # The REAL protection is not the free-VRAM number but closing ComfyUI every time
    # (see below): with another CUDA context present, even at 15290 MiB "free" the driver
    # fragments memory and the model spills into shared memory, dropping to ~6 tok/s.
    #
    # On LITE the margin is deliberately small (300 MiB) and the tolerance is wide: on an
    # 8 GB card the model already runs at the limit; llama.cpp keeps the big embedding tensor
    # on the CPU and absorbs a small overflow, still running at ~41 tok/s. Enlarging the
    # margin here would reject a working LITE install for nothing.
    $overheadMiB = if ($profileName -eq 'lite') { 300 } else { 600 }
    $neededMiB = $modelMiB + $overheadMiB
    # llama.cpp keeps the big embedding (token_embd) tensor on the CPU when VRAM is short
    # and tolerates a small overflow. On LITE the MTP head grows the file by ~464 MiB; on
    # an 8 GB card the model still loaded at ~165 MiB over and ran at ~41 tok/s. So a small
    # overflow is not blocked on LITE; the tolerance there is wide.
    # For MAX/FLASH (16 GB) the tolerance is ZERO: the 27B model already runs at the card's
    # limit and, with a CUDA context (ComfyUI) open, even a small overflow pushes a component
    # into shared memory and drops production to ~6 tok/s. That is why ComfyUI is always
    # closed before measuring (see below); an overflow is not accepted on tolerance.
    $toleranceMiB = if ($profileName -eq 'lite') { 1024 } else { 0 }
    if ($freeMiB -lt ($neededMiB - $toleranceMiB)) {
        throw ("Not enough free VRAM: $freeMiB MiB free, the `"$profileName`" profile " +
            "needs an estimated $neededMiB MiB at least (model ~$modelMiB MiB, cache/compute " +
            "margin $overheadMiB MiB). Close other applications using the GPU (browser hardware " +
            "acceleration, another MamidLabs process etc.) and try again, or " +
            "choose a smaller profile.")
    }
    if ($freeMiB -lt $neededMiB) {
        Write-Warning ("Free VRAM is $($neededMiB - $freeMiB) MiB below the estimate; " +
            "llama.cpp will keep the big embedding tensor on the CPU (a small speed loss).")
    }
}

# The only big CUDA context we create is ComfyUI's. Even with the models unloaded the
# context (plus driver-side reservation) holds ~300 MiB and, more importantly, leaves VRAM
# fragmented (measured on an RTX 5060 Ti).
# ComfyUI is NOT a single process: it forms a PAIR (a venv launcher python.exe + its real
# base-python child). A healthy single instance is therefore 2 processes, so process COUNT
# cannot decide "leak": counting >1 would close a healthy ComfyUI on every load. Instances
# are counted by "roots" (a process whose parent is not itself a ComfyUI match). A real leak
# (e.g. a leftover `--list` copy) adds an extra root. Measured: two ComfyUI instances -> MAX
# 32K spilled to 0.89 tok/s; a single instance -> 61 tok/s. `Stop-Leaks` keeps the instance
# listening on :8188 and closes only the extras; `Stop-IdleComfy` closes them all (explicit
# cleanup). The old `Select-Object -First 1` closed only one half of the pair.
function Get-ComfyProcesses {
    @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*ComfyUI\main.py*' })
}

# Closes ALL idle (empty-queue) ComfyUI processes. $true if at least one was closed.
# (For explicit cleanup: `-Action closecomfy`, the install and smoke-test hygiene.)
function Stop-IdleComfy {
    $procs = @(Get-ComfyProcesses)
    if ($procs.Count -eq 0) { return $false }
    try {
        $queue = Invoke-RestMethod 'http://127.0.0.1:8188/queue' -TimeoutSec 3 -ErrorAction Stop
        if (@($queue.queue_running).Count -gt 0 -or @($queue.queue_pending).Count -gt 0) { return $false }
    } catch { }  # A process that does not respond is unusable anyway; it can be closed.
    $killed = $false
    foreach ($comfy in $procs) {
        try {
            $process = [Diagnostics.Process]::GetProcessById([int]$comfy.ProcessId)
            $process.Kill($true)
            $process.WaitForExit(20000) | Out-Null
            $killed = $true
        } catch { }
    }
    # Force-kill anything that did not exit promptly (a pair/leak can close with a delay).
    foreach ($comfy in $procs) {
        if (Get-Process -Id ([int]$comfy.ProcessId) -ErrorAction SilentlyContinue) {
            Stop-Process -Id ([int]$comfy.ProcessId) -Force -ErrorAction SilentlyContinue
            $killed = $true
        }
    }
    Start-Sleep -Milliseconds 1500  # The driver frees VRAM with a slight delay.
    return $killed
}

# Instance roots (a matching process whose parent is not itself a matching process).
function Get-ComfyInstanceRoots($procs) {
    $ids = @($procs | ForEach-Object { [int]$_.ProcessId })
    @($procs | Where-Object { $ids -notcontains [int]$_.ParentProcessId })
}

# Is $procId part of the instance rooted at $rootId? (Parent chain within matches.)
function Test-ComfyUnderRoot($all, [int]$procId, [int]$rootId) {
    $cur = $procId
    for ($i = 0; $i -lt 8; $i++) {
        if ($cur -eq $rootId) { return $true }
        $m = $all | Where-Object { [int]$_.ProcessId -eq $cur } | Select-Object -First 1
        if (!$m) { return $false }
        $cur = [int]$m.ParentProcessId
    }
    return $false
}

# Closes only LEAKED/duplicate ComfyUI instances; a single healthy instance (with its pair)
# is left untouched. The instance listening on :8188 is kept. $true if a leak was closed.
function Stop-ComfyLeaks {
    $all = @(Get-ComfyProcesses)
    if ($all.Count -eq 0) { return $false }
    $roots = @(Get-ComfyInstanceRoots $all)
    if ($roots.Count -le 1) { return $false }  # single instance (may be a pair) -> leave it
    $listener = 0
    try { $listener = [int](Get-NetTCPConnection -LocalPort 8188 -State Listen -ErrorAction Stop | Select-Object -First 1).OwningProcess } catch { }
    $keepRoot = [int]$roots[0].ProcessId
    if ($listener) {
        foreach ($r in $roots) { if (Test-ComfyUnderRoot $all $listener ([int]$r.ProcessId)) { $keepRoot = [int]$r.ProcessId; break } }
    }
    $killed = $false
    foreach ($p in $all) {
        if (Test-ComfyUnderRoot $all ([int]$p.ProcessId) $keepRoot) { continue }  # kept instance
        try {
            $process = [Diagnostics.Process]::GetProcessById([int]$p.ProcessId)
            $process.Kill($true)
            $process.WaitForExit(15000) | Out-Null
            $killed = $true
        } catch { }
    }
    foreach ($p in $all) {
        if ((Test-ComfyUnderRoot $all ([int]$p.ProcessId) $keepRoot)) { continue }
        if (Get-Process -Id ([int]$p.ProcessId) -ErrorAction SilentlyContinue) { Stop-Process -Id ([int]$p.ProcessId) -Force -ErrorAction SilentlyContinue; $killed = $true }
    }
    if ($killed) { Start-Sleep -Milliseconds 1500 }
    return $killed
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

    # Resource hygiene: close a leftover ComfyUI. If the app crashed / was ended from
    # Task Manager, or the smoke test's UI step force-killed the app, ComfyUI can be left
    # running; loading the language model on top of it spills VRAM. Callers (uninstall,
    # smoke-test, Shutdown-All) trigger this explicitly.
    if ($Action -eq 'closecomfy') { [void](Stop-IdleComfy); 'ComfyUI closed (if idle).'; return }

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
    # ComfyUI lifecycle. DEFAULT OFF: ComfyUI runs only during image/video generation and is
    # closed when the job ends and before the language model loads — it holds neither VRAM nor RAM
    # while idle. Rationale: the LITE target machine is 8 GB VRAM / 16 GB RAM; the 9B model is
    # already at the card's limit (~7.5 GB), so the ComfyUI context (~300 MiB) plus the desktop
    # spills it. On a 16 GB card, LITE users who want it open between media jobs set
    # `settings/api.json` -> `comfyKeepAlive=true` (no ~27 s reopen). MAX/FLASH (27B) always close.
    # In API mode this script is not called; ComfyUI stays open after media.
    # A leaked/duplicate ComfyUI is cleaned in every profile first.
    $comfyKeepAlive = $false
    if ($settings -and $null -ne $settings.comfyKeepAlive) { $comfyKeepAlive = [bool]$settings.comfyKeepAlive }
    [void](Stop-ComfyLeaks)
    if (!$comfyKeepAlive -or $Profile -ne 'lite') { [void](Stop-IdleComfy) }
    # The previous process was already stopped above; free VRAM is measured here so the
    # memory freed by the old process is counted too.
    if (!$SkipMemoryCheck) {
        try { Test-MemoryBudget $model $Profile $projector }
        catch {
            # Not enough memory (ComfyUI open): close it if idle and retry; else fail.
            if ((Stop-IdleComfy)) { Test-MemoryBudget $model $Profile $projector } else { throw }
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
