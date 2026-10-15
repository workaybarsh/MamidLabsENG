param([hashtable]$JobData, [hashtable]$Shared)
$ErrorActionPreference='Stop'
# Derive the install root from this file's own location. IMPORTANT: this file is started with
# Start-ThreadJob -FilePath; that reads the target file as TEXT and runs it in a new
# runspace - $PSScriptRoot is ALWAYS EMPTY in that context (verified on a real machine).
# The primary source is the value mamidlabs-desktop.ps1/media-settings.ps1/hf-settings.ps1/profile-catalog.ps1
# pass explicitly via JobData.root.
$root = if ($JobData.root) { [string]$JobData.root } elseif ($PSScriptRoot) { Split-Path -Parent $PSScriptRoot } else { (Get-Location).Path }
$process=$null
try {
    $psi=[Diagnostics.ProcessStartInfo]::new()
    $psi.FileName=Join-Path $root 'venv\app\Scripts\python.exe'
    $worker='api-media.py'
    if($JobData.kind -eq 'huggingface'){$worker='hf-hub.py'}
    $psi.ArgumentList.Add((Join-Path $root ('agent\' + $worker)))
    $psi.UseShellExecute=$false;$psi.CreateNoWindow=$true
    $psi.RedirectStandardInput=$true;$psi.RedirectStandardOutput=$true;$psi.RedirectStandardError=$true
    $psi.StandardInputEncoding=[Text.UTF8Encoding]::new($false)
    $psi.StandardOutputEncoding=[Text.Encoding]::UTF8
    $process=[Diagnostics.Process]::Start($psi)
    $Shared.Process=$process
    $errors=$process.StandardError.ReadToEndAsync()
    $process.StandardInput.Write(($JobData|ConvertTo-Json -Depth 10 -Compress));$process.StandardInput.Close()
    while (!$process.StandardOutput.EndOfStream) {
        $line=$process.StandardOutput.ReadLine()
        if ($line) { $line | ConvertFrom-Json }
    }
    $process.WaitForExit()
    if ($Shared.Cancel) { [pscustomobject]@{kind='cancelled';text='API tracking stopped. The job at the provider may continue.'} }
    elseif ($process.ExitCode -ne 0) { throw 'The API generation process closed unexpectedly.' }
} catch {
    [pscustomobject]@{kind='error';text=$_.Exception.Message}
} finally {
    $Shared.Process=$null
    if ($process) { if(!$process.HasExited){$process.Kill($true)};$process.Dispose() }
}
