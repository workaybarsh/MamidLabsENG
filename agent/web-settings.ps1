# MamidLabs local web management window: set a password, local-network option, start/stop.
# The server (agent\web-server.ps1) reads the password from settings\api.json -> web.password
# itself (DPAPI), so the password never appears on a command line or in the process list.
function Get-WebServerProcess {
    @(Get-CimInstance Win32_Process -Filter "Name='pwsh.exe'" -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -and $_.CommandLine -like '*web-server.ps1*' })
}

function Get-WebSettingsValues {
    $web = if ($script:apiSettings) { $script:apiSettings.web } else { $null }
    $port = 8917
    $lan = $false
    $hasPassword = $false
    if ($web) {
        if ($web.port) { $port = [int]$web.port }
        if ($null -ne $web.lan) { $lan = [bool]$web.lan }
        if ($web.password) { $hasPassword = $true }
    }
    return @{ port = $port; lan = $lan; hasPassword = $hasPassword }
}

function Start-MamidWebServer([int]$Port, [bool]$Lan) {
    $manage = Join-Path $root 'agent\web-server.ps1'
    if (!(Test-Path -LiteralPath $manage -PathType Leaf)) { throw "web-server.ps1 not found: $manage" }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = [System.Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
    foreach ($argument in @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $manage, '-Root', $root, '-Port', [string]$Port)) {
        $psi.ArgumentList.Add($argument)
    }
    if ($Lan) { $psi.ArgumentList.Add('-Lan') }
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    return [System.Diagnostics.Process]::Start($psi)
}

function Stop-MamidWebServer {
    foreach ($proc in Get-WebServerProcess) {
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

function Show-WebSettings {
    if ($script:busy -or $script:imageBusy) { $metric.Text = 'Wait for the current job to finish before web management.'; return }
    $values = Get-WebSettingsValues
    $dlg = [Windows.Forms.Form]::new()
    $dlg.Text = 'MamidLabs · Web management'
    $dlg.Size = [Drawing.Size]::new(620, 430)
    $dlg.StartPosition = 'CenterParent'
    $dlg.BackColor = $colPanel; $dlg.ForeColor = $colText
    $dlg.FormBorderStyle = 'FixedDialog'; $dlg.MaximizeBox = $false; $dlg.MinimizeBox = $false

    $pwLabel = [Windows.Forms.Label]::new(); $pwLabel.Text = if ($values.hasPassword) { 'Password (saved; type to change)' } else { 'Password (required)' }
    $pwLabel.SetBounds(20, 16, 560, 22)
    $pw = [Windows.Forms.TextBox]::new(); $pw.SetBounds(20, 40, 560, 26); $pw.UseSystemPasswordChar = $true

    $portLabel = [Windows.Forms.Label]::new(); $portLabel.Text = 'Port'; $portLabel.SetBounds(20, 76, 120, 22)
    $portBox = [Windows.Forms.TextBox]::new(); $portBox.SetBounds(20, 100, 120, 26); $portBox.Text = [string]$values.port
    $lan = [Windows.Forms.CheckBox]::new(); $lan.Text = 'Expose to the local network (0.0.0.0 - needs admin)'; $lan.SetBounds(160, 100, 420, 26)
    $lan.Checked = $values.lan

    $note = [Windows.Forms.Label]::new()
    $note.SetBounds(20, 132, 560, 70)
    $note.Text = 'By default the server is reachable only from this computer (127.0.0.1). The password is stored encrypted, bound to the Windows account. Changing the password invalidates open sessions. Exposing it to the local network needs Windows admin rights / a URL ACL; anyone on the network can reach the port (still password protected, but the risk is higher).'
    $link = [Windows.Forms.LinkLabel]::new(); $link.SetBounds(20, 200, 560, 22)
    $link.Text = 'Open in a browser: http://127.0.0.1:' + [string]$values.port + '/'
    $link.Tag = $values.port
    $link.Add_Click({ try { Start-Process -FilePath ('http://127.0.0.1:' + [string]$this.Tag + '/') } catch { } })

    $status = [Windows.Forms.Label]::new(); $status.SetBounds(20, 226, 560, 44)

    $buttons = @(
        @{ text = 'Save'; x = 20; action = 'save' },
        @{ text = 'Start'; x = 155; action = 'start' },
        @{ text = 'Stop'; x = 290; action = 'stop' },
        @{ text = 'Close'; x = 425; action = 'close' }
    )
    foreach ($b in $buttons) {
        $btn = [Windows.Forms.Button]::new(); $btn.Text = $b.text; $btn.SetBounds([int]$b.x, 280, 125, 34)
        $btn.Tag = @{ action = $b.action }
        $btn.Add_Click({
            $action = $this.Tag.action
            try {
                if ($action -eq 'close') { $this.Parent.Close(); return }
                if ($pw.Text.Trim()) {
                    if (!$script:apiSettings.web) { $script:apiSettings.web = @{} }
                    $script:apiSettings.web.password = (ConvertFrom-SecureString (ConvertTo-SecureString $pw.Text.Trim() -AsPlainText -Force))
                    $pw.Text = ''
                }
                if (!$script:apiSettings.web) { $script:apiSettings.web = @{} }
                $script:apiSettings.web.port = [int]$portBox.Text
                $script:apiSettings.web.lan = [bool]$lan.Checked
                Save-ApiSettings
                if ($action -eq 'save') { $status.Text = 'Saved.'; return }
                if ($action -eq 'stop') { Stop-MamidWebServer; $status.Text = 'Web management stopped.'; return }
                if (!$script:apiSettings.web.password) { $status.Text = 'Enter and save a password first.'; return }
                $existing = @(Get-WebServerProcess)
                if ($existing.Count -gt 0) { Stop-MamidWebServer; Start-Sleep -Seconds 1 }
                [void](Start-MamidWebServer ([int]$portBox.Text) ([bool]$lan.Checked))
                $link.Text = 'Open in a browser: http://127.0.0.1:' + $portBox.Text + '/'
                $link.Tag = [int]$portBox.Text
                $status.Text = 'Web management started. Open the link in a browser and enter the password.'
            } catch { $status.Text = $_.Exception.Message }
        })
        $dlg.Controls.Add($btn)
    }
    $dlg.Controls.AddRange(@($pwLabel, $pw, $portLabel, $portBox, $lan, $note, $link, $status))
    [void]$dlg.ShowDialog($form)
    $dlg.Dispose()
}
