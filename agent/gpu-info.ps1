# NVIDIA free-VRAM measurement - the SINGLE source (manage / media / smoke all use it).
#
# Why strict parsing: the old code used `-replace '\D',''`, which concatenated EVERY digit.
# That could turn driver-version numbers in an error message (e.g. "580.88") into a huge
# "free VRAM" value, drop the sign of `-5`, and produce 0 for `[N/A]`/empty output. A 0 treated
# as a real measurement made ComfyUI get killed for nothing; a bogus LARGE value bypassed the
# memory gate and let the model spill into shared memory (chat ~40 -> ~27 tok/s). So only an
# exact integer (optionally followed by "MiB") is accepted; everything else is "unmeasurable"
# = $null.

# "12345" / "12345 MiB" -> 12345 ; [N/A], empty, error text, negative -> $null
function ConvertFrom-MiBText([string]$text) {
    if ($null -eq $text) { return $null }
    $line = @([string]$text -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -First 1)
    if ($line.Count -eq 0) { return $null }
    if ($line[0] -notmatch '^\s*(\d{1,9})\s*(?:MiB)?\s*$') { return $null }
    return [int]$Matches[1]
}

# The card's current free VRAM (MiB). With multiple GPUs the FIRST card (the app loads one card).
# nvidia-smi missing/unreadable/error -> $null. Callers must treat $null as "no measurement",
# not as 0.
function Get-NvidiaFreeVramMiB {
    $smi = (Get-Command nvidia-smi -ErrorAction SilentlyContinue).Source
    if (!$smi) { return $null }
    $raw = $null
    try { $raw = & $smi --query-gpu=memory.free --format=csv,noheader,nounits 2>$null } catch { return $null }
    if ($null -eq $raw) { return $null }
    $first = @($raw | Select-Object -First 1)
    if ($first.Count -eq 0) { return $null }
    return (ConvertFrom-MiBText ([string]$first[0]))
}
