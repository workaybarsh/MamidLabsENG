# The single owner of the flow that unloads the language model from VRAM, starts/reuses
# ComfyUI and reloads the language model after generation. imagegen-run.ps1 and
# videogen-run.ps1 both use this code.
#
# The calling file must define an 'Emit([string]$kind, [string]$text, $data = $null)'
# function; the functions below send their status messages through it.

. (Join-Path $PSScriptRoot 'process-launch.ps1')
. (Join-Path $PSScriptRoot 'media-paths.ps1')
. (Join-Path $PSScriptRoot 'gpu-info.ps1')

function Stop-LocalModelForMedia([string]$manage) {
    Emit 'status' 'Unloading the language model from VRAM...'
    & $manage -Action stop | Out-Null
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while ([DateTime]::UtcNow -lt $deadline -and @(Get-Process -Name 'llama-server' -ErrorAction SilentlyContinue).Count -gt 0) {
        Start-Sleep -Milliseconds 300
    }
}

# Starting ComfyUI from scratch for every job takes ~20 s (measured on an 8 GB VRAM / 16 GB RAM
# machine; nothing is loaded into VRAM during that time). So after a successful job the
# process is not closed, only the models are unloaded; the next image/video uses the same
# process. On the error/cancel path the process is still killed so no broken state carries over.
function Get-RunningComfy {
    $found = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*ComfyUI\main.py*' } | Select-Object -First 1
    if (!$found) { return $null }
    try { return [Diagnostics.Process]::GetProcessById([int]$found.ProcessId) } catch { return $null }
}

# ComfyUI is a process PAIR (a venv launcher + a base-python child), so process COUNT cannot
# decide "leak": a healthy single instance is 2 processes. Instances are counted by roots
# (a process whose parent is not a ComfyUI match). If there is more than one root, the leaked
# instances are closed; the single healthy instance is left untouched (otherwise every media
# job would needlessly close ComfyUI).
function Get-ComfyLeakProcesses {
    @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*ComfyUI\main.py*' })
}
function Clear-ComfyLeaks {
    $all = @(Get-ComfyLeakProcesses)
    if ($all.Count -eq 0) { return }
    $ids = @($all | ForEach-Object { [int]$_.ProcessId })
    $roots = @($all | Where-Object { $ids -notcontains [int]$_.ParentProcessId })
    if ($roots.Count -le 1) { return }  # single instance (may be a pair) -> leave it
    $listener = 0
    try { $listener = [int](Get-NetTCPConnection -LocalPort 8188 -State Listen -ErrorAction Stop | Select-Object -First 1).OwningProcess } catch { }
    $keepRoot = [int]$roots[0].ProcessId
    if ($listener) {
        foreach ($r in $roots) {
            $cur = $listener; $hit = $false
            for ($i = 0; $i -lt 8; $i++) {
                if ($cur -eq [int]$r.ProcessId) { $hit = $true; break }
                $m = $all | Where-Object { [int]$_.ProcessId -eq $cur } | Select-Object -First 1
                if (!$m) { break }
                $cur = [int]$m.ParentProcessId
            }
            if ($hit) { $keepRoot = [int]$r.ProcessId; break }
        }
    }
    foreach ($p in $all) {
        $cur = [int]$p.ProcessId; $under = $false
        for ($i = 0; $i -lt 8; $i++) {
            if ($cur -eq $keepRoot) { $under = $true; break }
            $m = $all | Where-Object { [int]$_.ProcessId -eq $cur } | Select-Object -First 1
            if (!$m) { break }
            $cur = [int]$m.ParentProcessId
        }
        if ($under) { continue }
        try { $proc = [Diagnostics.Process]::GetProcessById([int]$p.ProcessId); $proc.Kill($true); $proc.WaitForExit(15000) | Out-Null } catch { }
    }
    foreach ($p in $all) {
        $cur = [int]$p.ProcessId; $under = $false
        for ($i = 0; $i -lt 8; $i++) {
            if ($cur -eq $keepRoot) { $under = $true; break }
            $m = $all | Where-Object { [int]$_.ProcessId -eq $cur } | Select-Object -First 1
            if (!$m) { break }
            $cur = [int]$m.ParentProcessId
        }
        if ($under) { continue }
        if (Get-Process -Id ([int]$p.ProcessId) -ErrorAction SilentlyContinue) { Stop-Process -Id ([int]$p.ProcessId) -Force -ErrorAction SilentlyContinue }
    }
    Start-Sleep -Milliseconds 1500
}

function Start-OrReuseComfy([string]$python, [string]$comfyDir, [string]$comfyOut, [string]$comfyIn,
                            [string]$logs, [string]$comfyUrl, [bool]$lite, [int]$timeoutSec, [hashtable]$Shared) {
    # External model folders (files used in place without copying); see media-paths.ps1.
    $extraPaths = Join-Path (Split-Path -Parent $comfyDir) 'settings\comfy-extra-models.yaml'
    $hasExtra = Test-Path -LiteralPath $extraPaths -PathType Leaf
    # Clean up leaked/duplicate copies (single-instance rule).
    Clear-ComfyLeaks
    $existing = Get-RunningComfy
    if ($existing) {
        # If the open process started before the external folder list changed it cannot see the new folder.
        $stale = $false
        try { $stale = $hasExtra -and (Get-Item -LiteralPath $extraPaths).LastWriteTime -gt $existing.StartTime } catch { }
        if ($stale) {
            try { $existing.Kill($true); $existing.WaitForExit(20000) | Out-Null } catch { }
            $existing = $null
        }
    }
    if ($existing) {
        try {
            Invoke-RestMethod "$comfyUrl/system_stats" -TimeoutSec 3 -ErrorAction Stop | Out-Null
            $Shared.Process = $existing
            return $existing
        } catch {
            # A process that does not respond holds the port; it is closed and a new one is started.
            try { $existing.Kill($true); $existing.WaitForExit(20000) | Out-Null } catch { }
        }
    }
    $comfyArgs = @(
        (Join-Path $comfyDir 'main.py'), '--listen', '127.0.0.1', '--port', '8188',
        '--disable-auto-launch', '--dont-print-server',
        '--output-directory', $comfyOut, '--input-directory', $comfyIn
    )
    if ($lite) { $comfyArgs += '--lowvram' }
    if ($hasExtra) { $comfyArgs += @('--extra-model-paths-config', $extraPaths) }
    # Since ComfyUI stays open between jobs it must not inherit the caller's pipe.
    $comfy = Start-DetachedProcess $python $comfyArgs $comfyDir `
        (Join-Path $logs 'comfyui.stdout.log') (Join-Path $logs 'comfyui.stderr.log')
    $Shared.Process = $comfy
    $deadline = [DateTime]::UtcNow.AddSeconds($timeoutSec)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($Shared.Cancel) { throw 'Operation stopped.' }
        if ($comfy.HasExited) { throw 'ComfyUI could not be started; see the logs\comfyui.stderr.log file.' }
        try { Invoke-RestMethod "$comfyUrl/system_stats" -TimeoutSec 2 -ErrorAction Stop | Out-Null; return $comfy } catch { }
        Start-Sleep -Milliseconds 500
    }
    throw "ComfyUI was not ready in $timeoutSec seconds."
}

# Instant free VRAM (MiB) via nvidia-smi. Returns $null if unavailable/unreadable; the
# caller then skips the extra check (llama-server gives the real error anyway).
# Measurement comes from the SINGLE source (agent\gpu-info.ps1): strict parsing, so
# [N/A]/error text/negative -> $null. IMPORTANT: it used to return 0 on bad output and
# Release-Comfy read that as "not enough VRAM" and killed ComfyUI for nothing (~30 s reopen).
function Get-MediaFreeVramMiB {
    return (Get-NvidiaFreeVramMiB)
}

# Unloads the models and the cache; before the language model is reloaded it waits until VRAM
# is really freed (the /free flag is handled in ComfyUI's job loop).
# If it does not free up, the process is closed - so the language model is not left without VRAM.
#
# $minFreeMiB: the minimum free VRAM needed for the language model to fit fully. Even when
# ComfyUI /free unloads its model cache (torch_vram_total < 512 MB), the CUDA context and the
# driver-side reservation may hold VRAM for a while; looking only at the torch counter could
# let the language model spill into shared memory and drop to ~6 tok/s (measured after media).
# So when $minFreeMiB > 0 the real free VRAM is also verified; if it is not enough, ComfyUI is
# closed and releases VRAM together with its context.
function Release-Comfy($process, [string]$comfyUrl, [int]$minFreeMiB = 0) {
    try {
        Invoke-RestMethod "$comfyUrl/free" -Method Post -ContentType 'application/json' `
            -Body '{"unload_models":true,"free_memory":true}' -TimeoutSec 10 | Out-Null
        $deadline = [DateTime]::UtcNow.AddSeconds(20)
        while ([DateTime]::UtcNow -lt $deadline) {
            Start-Sleep -Milliseconds 500
            $device = @((Invoke-RestMethod "$comfyUrl/system_stats" -TimeoutSec 5).devices)[0]
            if ($device -and [double]$device.torch_vram_total -lt 512MB) {
                if ($minFreeMiB -le 0) { return }
                $free = Get-MediaFreeVramMiB
                if ($null -eq $free -or $free -ge $minFreeMiB) { return }
            }
        }
    } catch { }
    if ($process -and !$process.HasExited) {
        try { $process.Kill($true) } catch { }
        try { $process.WaitForExit(20000) | Out-Null } catch { }
    }
}

# If $swallowError is $false (success path): the error is thrown as is and caught by the
# caller's own catch block. If $swallowError is $true (error/cancel path, a second
# attempt): the error is swallowed and returned as text so the caller can show it together
# with the original error - so that if the restore attempt ITSELF fails it is not silently
# hidden from the user (#16).
function Restore-LocalModelForMedia([string]$manage, [string]$restoreProfile, [bool]$swallowError) {
    if (!$restoreProfile) { return $null }
    Emit 'status' "Reloading the language model ($restoreProfile)..."
    if ($swallowError) {
        try { & $manage -Action start -Profile $restoreProfile | Out-Null; return $null }
        catch { return $_.Exception.Message }
    }
    & $manage -Action start -Profile $restoreProfile | Out-Null
    return $null
}
