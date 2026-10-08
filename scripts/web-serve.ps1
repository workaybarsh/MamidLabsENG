#Requires -Version 5.1
<#
.SYNOPSIS
    Starts/stops the MamidLabs local web management (browser access).

.DESCRIPTION
    Does the same as the app's "Web management" window: starts agent\web-server.ps1 in the
    background. The server reads the password from settings\api.json -> web.password
    (Windows DPAPI); a password must first be set from the app. By default it is reachable
    only from this computer; -Lan exposes it to the local network (needs Windows admin
    rights / a URL ACL).

.EXAMPLE
    runtime\pwsh\pwsh.exe -File scripts\web-serve.ps1 -Start
    runtime\pwsh\pwsh.exe -File scripts\web-serve.ps1 -Start -Port 8917 -Lan
    runtime\pwsh\pwsh.exe -File scripts\web-serve.ps1 -Stop
#>
param(
    [switch]$Start,
    [switch]$Stop,
    [int]$Port = 0,
    [switch]$Lan,
    [string]$Root
)

$ErrorActionPreference = 'Stop'
if (!$Root) { $Root = Split-Path -Parent $PSScriptRoot }

function Get-WebServerProcess {
    @(Get-CimInstance Win32_Process -Filter "Name='pwsh.exe'" -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -and $_.CommandLine -like '*web-server.ps1*' })
}

if ($Stop) {
    foreach ($proc in Get-WebServerProcess) { Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue }
    Write-Host 'Web management stopped.'
    exit 0
}

if (!$Start) { throw 'Pick an action: -Start or -Stop.' }

$server = Join-Path $Root 'agent\web-server.ps1'
if (!(Test-Path -LiteralPath $server -PathType Leaf)) { throw "Not found: $server" }

$port = $Port
$lanFlag = [bool]$Lan
$settingsPath = Join-Path $Root 'settings\api.json'
if (Test-Path -LiteralPath $settingsPath -PathType Leaf) {
    try {
        $settings = Get-Content -LiteralPath $settingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($settings.web) {
            if (!$port -and $settings.web.port) { $port = [int]$settings.web.port }
            if (!$Lan -and $null -ne $settings.web.lan) { $lanFlag = [bool]$settings.web.lan }
            if (!$settings.web.password) { throw 'Set a web password from the app first.' }
        }
    } catch { Write-Host $_.Exception.Message }
}
if (!$port) { $port = 8917 }

foreach ($proc in Get-WebServerProcess) { Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 500

$pwsh = [Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
$args = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $server, '-Root', $Root, '-Port', [string]$port)
if ($lanFlag) { $args += '-Lan' }
Start-Process -FilePath $pwsh -ArgumentList $args -WindowStyle Hidden
Start-Sleep -Seconds 2
Write-Host ("Web management started: http://127.0.0.1:{0}/" -f $port)
Write-Host 'Open it in a browser and enter the password.'
exit 0
