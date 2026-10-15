<#
.SYNOPSIS
    Measures the cause of the "I sent the message and it keeps me waiting" complaint.

.DESCRIPTION
    Answers three questions with data:
      1) Does the wait come from prefix (prompt) processing or from generation?
      2) Does the server use the KV cache, or does it reprocess the whole context every turn?
      3) Does the model fit in VRAM, or does it spill into shared system memory?

    Sends the same chat in two turns: in the second turn only the last message is added.
    If the cache works, the "reprocessed tokens" count stays small in the second turn.

.EXAMPLE
    pwsh -File scripts\diagnose-latency.ps1
    pwsh -File scripts\diagnose-latency.ps1 -FillTokens 8000
#>
param(
    [string]$Endpoint = 'http://127.0.0.1:8080',
    # Measures long-context behavior by artificially growing the chat.
    [int]$FillTokens = 4000,
    [ValidateSet('low', 'medium', 'xhigh')][string]$Effort = 'low',
    [string]$LogDir = (Join-Path (Split-Path -Parent $PSScriptRoot) 'logs')
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Write-Head([string]$text) {
    Write-Host ''
    Write-Host $text -ForegroundColor Cyan
    Write-Host ('-' * $text.Length) -ForegroundColor DarkCyan
}

# ---------------------------------------------------------------- 1. GPU / VRAM
Write-Head '1) GPU and VRAM'
$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($smi) {
    $row = (& nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits) -split '\s*,\s*'
    $used = [int]$row[1]; $total = [int]$row[2]
    Write-Host ("GPU        : {0}" -f $row[0])
    Write-Host ("VRAM       : {0} / {1} MiB  (%{2:0})" -f $used, $total, (100.0 * $used / [math]::Max(1, $total)))
    if ($used -gt ($total * 0.95)) {
        Write-Host 'WARNING: VRAM is full. The Windows driver moves the overflow into system RAM (shared memory);' -ForegroundColor Yellow
        Write-Host '         in that case both prefix processing and generation slow down many times over.' -ForegroundColor Yellow
    }
} else {
    Write-Host 'nvidia-smi not found; the VRAM measurement was skipped.' -ForegroundColor DarkYellow
}
# Shared memory use: if the memory committed by the llama-server process is larger than
# VRAM, an overflow is very likely.
$server = Get-Process -Name 'llama-server' -ErrorAction SilentlyContinue
if ($server) {
    Write-Host ("llama-server: PID {0}, working set {1:0} MiB" -f $server.Id, ($server.WorkingSet64 / 1MB))
} else {
    Write-Host 'llama-server is not running.' -ForegroundColor Yellow
    return
}

# ---------------------------------------------------------------- 2. request measurement
function Measure-Turn([object[]]$messages, [string]$label) {
    $body = @{
        messages = $messages
        max_tokens = 48
        stream = $true
        cache_prompt = $true
        timings_per_token = $true
        stream_options = @{ include_usage = $true }
        temperature = 0.2
        reasoning_effort = $Effort
        chat_template_kwargs = @{ reasoning_effort = $Effort }
    } | ConvertTo-Json -Depth 20 -Compress

    $handler = [Net.Http.HttpClientHandler]::new()
    $handler.UseProxy = $false
    $http = [Net.Http.HttpClient]::new($handler)
    $http.Timeout = [TimeSpan]::FromMinutes(10)
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $ttft = $null
    $timings = $null
    try {
        $request = [Net.Http.HttpRequestMessage]::new([Net.Http.HttpMethod]::Post, "$Endpoint/v1/chat/completions")
        $request.Content = [Net.Http.StringContent]::new($body, [Text.Encoding]::UTF8, 'application/json')
        $response = $http.SendAsync($request, [Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
        if (!$response.IsSuccessStatusCode) {
            throw ("Server error {0}: {1}" -f [int]$response.StatusCode, $response.Content.ReadAsStringAsync().GetAwaiter().GetResult())
        }
        $reader = [IO.StreamReader]::new($response.Content.ReadAsStreamAsync().GetAwaiter().GetResult(), [Text.Encoding]::UTF8)
        while ($null -ne ($line = $reader.ReadLine())) {
            if (!$line.StartsWith('data:')) { continue }
            $data = $line.Substring(5).Trim()
            if ($data -eq '[DONE]') { break }
            try { $chunk = $data | ConvertFrom-Json -ErrorAction Stop } catch { continue }
            if ($chunk.timings) { $timings = $chunk.timings }
            $delta = $chunk.choices[0].delta
            if (!$ttft -and ($delta.content -or $delta.reasoning_content)) { $ttft = $clock.ElapsedMilliseconds }
        }
        $reader.Dispose(); $response.Dispose(); $request.Dispose()
    } finally { $http.Dispose() }

    $cached = [int]$timings.cache_n
    $processed = [int]$timings.prompt_n
    Write-Host ''
    Write-Host $label -ForegroundColor White
    Write-Host ("  first token          : {0:0.0} s" -f (([double]$ttft) / 1000.0))
    Write-Host ("  prefix tokens processed: {0}" -f $processed)
    Write-Host ("  from cache           : {0}" -f $cached)
    if ($timings.prompt_ms) {
        Write-Host ("  prefix processing time: {0:0.0} s  ({1:0} token/s)" -f ([double]$timings.prompt_ms / 1000.0), [double]$timings.prompt_per_second)
    }
    if ($timings.predicted_per_second) {
        Write-Host ("  generation speed     : {0:0.0} token/s" -f [double]$timings.predicted_per_second)
    }
    return @{ ttft = [double]$ttft; processed = $processed; cached = $cached; timings = $timings }
}

Write-Head "2) Prefix processing and KV cache (~$FillTokens tokens of filler)"
# Filler text: must be the same on every run so both turns share the same prefix.
$filler = (1..([math]::Max(1, [int]($FillTokens / 12)))) | ForEach-Object {
    "Line $_ : this text is a fixed measurement text used to fill the context."
}
$base = @(
    @{ role = 'system'; content = 'Answer briefly.' }
    @{ role = 'user'; content = ($filler -join "`n") }
    @{ role = 'assistant'; content = 'Got the text.' }
)
$first = Measure-Turn ($base + @(@{ role = 'user'; content = 'Answer in one word: are you ready?' })) 'Turn 1 (cold cache)'
$second = Measure-Turn ($base + @(
    @{ role = 'user'; content = 'Answer in one word: are you ready?' }
    @{ role = 'assistant'; content = 'Ready.' }
    @{ role = 'user'; content = 'Now answer again in one word.' }
)) 'Turn 2 (only the last message added)'

# ---------------------------------------------------------------- 3. interpretation
Write-Head '3) Result'
$reuse = $second.cached -gt ($first.processed * 0.5)
if ($reuse) {
    Write-Host 'The KV cache WORKS: the old context was not reprocessed in the second turn.' -ForegroundColor Green
} else {
    Write-Host 'The KV cache is NOT USED: the whole context was reprocessed from scratch in the second turn.' -ForegroundColor Red
    Write-Host 'The cause is usually a change at the START of the prompt: the system prompt, the tool definitions' -ForegroundColor Yellow
    Write-Host '(when chips are toggled) or a rewrite of the history (compaction).' -ForegroundColor Yellow
}
$prefillShare = if ($second.ttft -gt 0 -and $second.timings.prompt_ms) { [double]$second.timings.prompt_ms / $second.ttft } else { 0 }
if ($prefillShare -gt 0.6) {
    Write-Host ("The wait comes mostly from prefix processing ({0:0}% of the first token)." -f ($prefillShare * 100)) -ForegroundColor Yellow
}
if ($second.timings.prompt_per_second -and [double]$second.timings.prompt_per_second -lt 200) {
    Write-Host ("The prefix processing speed is low ({0:0} token/s). Layers that do not fit on the GPU or a VRAM overflow are the typical cause." -f [double]$second.timings.prompt_per_second) -ForegroundColor Yellow
}
if ($second.timings.predicted_per_second -and [double]$second.timings.predicted_per_second -lt 12) {
    Write-Host ("The generation speed is low too ({0:0.0} token/s). This is a sign of VRAM overflow/shared memory." -f [double]$second.timings.predicted_per_second) -ForegroundColor Red
}

# ---------------------------------------------------------------- 4. server log
Write-Head '4) Traces of reprocessing in the server log'
$logs = @(Get-ChildItem -LiteralPath $LogDir -Filter 'mamidlabs-*.std*.log' -File -ErrorAction SilentlyContinue)
if (!$logs) { Write-Host "No log found: $LogDir" -ForegroundColor DarkYellow }
foreach ($log in $logs) {
    $hits = @(Select-String -LiteralPath $log.FullName -Pattern 'forcing full prompt re-processing|n_past|prompt processing progress|cache_n|slot released' -ErrorAction SilentlyContinue | Select-Object -Last 8)
    if ($hits) {
        Write-Host $log.Name -ForegroundColor White
        foreach ($hit in $hits) { Write-Host ('  ' + $hit.Line.Trim()) }
    }
}
