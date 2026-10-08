# Profile catalog: which files each profile needs, whether the files are
# installed, and downloading the missing ones from Hugging Face.
# The catalog data lives in agent/profiles.json; rows with an empty repo field are
# installed manually, rows with a repo can be downloaded from the app.

function Get-ProfileCatalog {
    if ($script:profileCatalog) { return $script:profileCatalog }
    $path = Join-Path $root 'agent/profiles.json'
    if (!(Test-Path -LiteralPath $path -PathType Leaf)) { $script:profileCatalog = @(); return $script:profileCatalog }
    try {
        $script:profileCatalog = @((Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json).profiles)
    } catch {
        $script:profileCatalog = @()
    }
    return $script:profileCatalog
}

# MAX and FLASH download exactly the same files (identical "target" paths); in the
# user-facing catalog (Profiles menu + Profile manager window) FLASH is the sole
# representative (labeled "Qwen3.8 27B" in agent/profiles.json). Switching between MAX and
# FLASH at runtime is done from the model bar (model-registry.ps1's builtin-max/builtin-flash).
# This filter is for DISPLAY ONLY: lookups by id (Get-ProfileEntry, Get-ProfileState,
# Test-LocalProfileChatFilesPresent) must see the full catalog, because the model bar also
# calls them for 'max'.
function Get-ProfileCatalogForDisplay {
    return @(Get-ProfileCatalog | Where-Object { $_.id -ne 'max' })
}

function Get-ProfileEntry([string]$id) {
    return @(Get-ProfileCatalog | Where-Object { $_.id -eq $id })[0]
}

function Format-ByteSize([long]$bytes) {
    if ($bytes -le 0) { return '—' }
    if ($bytes -ge 1073741824) { return ('{0:N1} GB' -f ($bytes / 1073741824)) }
    return ('{0:N0} MB' -f ($bytes / 1048576))
}

# Does the GGUF header contain an MTP/nextn block? Used to detect that an existing
# (MTP-less) model still needs the head added without re-downloading (see agent/mtp_inject.py).
function Test-GgufHasNextn([string]$Path) {
    try {
        $fs = [IO.File]::OpenRead($Path)
        try {
            $buffer = [byte[]]::new(16777216)
            $read = $fs.Read($buffer, 0, $buffer.Length)
        }
        finally { $fs.Dispose() }
        return ([Text.Encoding]::ASCII.GetString($buffer, 0, $read)).Contains('nextn')
    }
    catch { return $false }
}

# Looks for a profile's files on disk; splits the missing ones into downloadable and
# to-be-installed-manually. No size comparison is done: if a download was left half-done
# the Hugging Face client resumes where it left off.
function Get-ProfileState([string]$id) {
    $entry = Get-ProfileEntry $id
    $state = @{ present = @(); missingDownloadable = @(); missingManual = @() }
    if (!$entry) { return $state }
    foreach ($file in @($entry.files)) {
        $full = Join-Path $root ([string]$file.target)
        if (Test-Path -LiteralPath $full -PathType Leaf) {
            # An existing model whose MTP head is missing is reprocessed (added without re-downloading).
            if ($file.mtp -and !(Test-GgufHasNextn $full)) { $state.missingDownloadable += $file }
            else { $state.present += $file }
        }
        elseif ([string]$file.repo) { $state.missingDownloadable += $file }
        else { $state.missingManual += $file }
    }
    return $state
}

# Checks whether a profile's CHAT-required files (language model + mmproj) are on disk.
# ComfyUI media (image/video) models are deliberately excluded: the install may have used
# -SkipMedia and they are not needed for chat to work. Used so the model bar at the bottom
# only lists a profile that can actually be loaded (see Update-ProviderSelection in
# model-registry.ps1).
function Test-LocalProfileChatFilesPresent([string]$id) {
    $entry = Get-ProfileEntry $id
    if (!$entry) { return $false }
    $chatFiles = @($entry.files | Where-Object { [string]$_.target -notmatch '^ComfyUI[\\/]' })
    if (!$chatFiles.Count) { return $false }
    foreach ($file in $chatFiles) {
        $full = Join-Path $root ([string]$file.target)
        if (!(Test-Path -LiteralPath $full -PathType Leaf)) { return $false }
    }
    return $true
}

# The update check reads the local version from the uninstall key Inno Setup writes
# ({AppId}_is1). The AppId must match packaging/windows/mamidlabs.iss; the TR and ENG
# editions have separate AppIds and separate GitHub repositories (see AGENTS.md, pitfalls).
$script:MamidLabsAppId = '{BB5BDCB4-3C19-4599-88C4-5CEA1A7942CB}'
$script:MamidLabsRepo = 'workaybarsh/MamidLabsENG'

function Get-InstalledMamidLabsVersion {
    foreach ($hive in @('HKCU:', 'HKLM:')) {
        $key = "$hive\Software\Microsoft\Windows\CurrentVersion\Uninstall\$($script:MamidLabsAppId)_is1"
        if (Test-Path -LiteralPath $key) {
            try {
                $v = (Get-ItemProperty -LiteralPath $key -Name DisplayVersion -ErrorAction Stop).DisplayVersion
                if ($v) { return $v }
            } catch { }
        }
    }
    return $null
}

# Compares the latest GitHub release with the local version (Inno's own uninstall
# entry). Result: @{ available; version; downloadUrl; size; localVersion; error }. On a
# network failure 'error' is set instead of throwing - so the button in the Profiles
# window can show a short status instead of a raw error message.
function Test-MamidLabsUpdateAvailable {
    $local = Get-InstalledMamidLabsVersion
    try {
        $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$($script:MamidLabsRepo)/releases/latest" `
            -Headers @{ 'User-Agent' = 'MamidLabs-Updater'; 'Accept' = 'application/vnd.github+json' } -TimeoutSec 20 -ErrorAction Stop
    } catch {
        return @{ available = $false; error = 'Could not check (no internet?)'; localVersion = $local }
    }
    $remoteTag = [string]$release.tag_name
    $remoteVersion = $remoteTag.TrimStart('v', 'V')
    $asset = @($release.assets | Where-Object { $_.name -like '*Setup*.exe' }) | Select-Object -First 1
    if (!$asset) { return @{ available = $false; error = 'Setup file not found'; localVersion = $local } }
    $isNewer = $true
    if ($local) {
        try { $isNewer = ([version]$remoteVersion) -gt ([version]$local) }
        catch { $isNewer = ($remoteVersion -ne $local) }
    }
    return @{ available = $isNewer; version = $remoteVersion; downloadUrl = [string]$asset.browser_download_url; size = [long]$asset.size; localVersion = $local }
}

function Get-ProfileStatusLabel([string]$id) {
    $state = Get-ProfileState $id
    $downloadable = @($state.missingDownloadable).Count
    $manual = @($state.missingManual).Count
    if ($downloadable + $manual -eq 0) { return 'installed' }
    if ($manual -eq 0) { return "$downloadable files to download" }
    return "$($downloadable + $manual) files missing"
}

# Reflects the choice in the model box at the bottom right; the box's own event loads the model.
function Switch-ToProfile([string]$id) {
    if ($script:busy -or $script:imageBusy -or $script:pendingSend) { $metric.Text = 'Wait for the current operation to finish first.'; return $false }
    $index = -1
    for ($i = 0; $i -lt $script:modelEntries.Count; $i++) {
        if ($script:modelEntries[$i].id -eq "builtin-$id") { $index = $i; break }
    }
    if ($index -lt 0) {
        # It may be hidden from the model bar entirely because its files are missing (see
        # Update-ProviderSelection); the user saw the missing-file warning here and chose to
        # switch anyway, so it is added to the list and selected temporarily.
        $def = @(Get-BuiltinProfileDefinitions | Where-Object { $_.profile -eq $id })[0]
        if (!$def) { $metric.Text = 'The profile was not found in the model list.'; return $false }
        $script:modelEntries += @{ id = $def.id; provider = 'local'; label = $def.label; profile = $def.profile; contextLimit = 32768 }
        [void]$modelChoice.Items.Add($def.label)
        $index = $script:modelEntries.Count - 1
    }
    if ($modelChoice.SelectedIndex -eq $index) { $metric.Text = "$($script:modelEntries[$index].label) is already selected."; return $true }
    $modelChoice.SelectedIndex = $index
    return $true
}

function Start-ProfileDownload([string]$id) {
    if ($script:busy -or $script:imageBusy) { return 'Wait for the current operation to finish first.' }
    $state = Get-ProfileState $id
    $missing = @($state.missingDownloadable)
    if (!$missing.Count) { return 'There are no files to download.' }
    $files = @()
    foreach ($file in $missing) {
        $files += @{
            repo = [string]$file.repo
            filename = [string]$file.filename
            revision = [string]$file.revision
            target = (Join-Path $root ([string]$file.target))
            label = [string]$file.label
            bytes = [long]$file.bytes
            mtp = $file.mtp
        }
    }
    $total = ($missing | Measure-Object -Property bytes -Sum).Sum
    $data = @{ kind = 'huggingface'; action = 'download'; files = $files; apiKey = (Get-ProviderKey 'huggingface'); root = $root }
    $script:activeBubble = New-Bubble 'assistant'
    $script:activeBubble.body.Text = "Downloading profile files: $($files.Count) files, about $(Format-ByteSize $total)."
    $script:sharedMedia = [hashtable]::Synchronized(@{ Cancel = $false; Process = $null })
    $script:imageJob = Start-ThreadJob -FilePath (Join-Path $root 'agent/api-media-run.ps1') -ArgumentList $data, $script:sharedMedia
    $script:imageBusy = $true
    $send.Enabled = $true
    $send.Text = 'Stop'
    Relayout-Chat
    return $null
}

function Show-ProfileManager([string]$focusId) {
    $catalog = @(Get-ProfileCatalogForDisplay)
    if (!$catalog.Count) { $metric.Text = 'Could not read the profile catalog: agent/profiles.json'; return }

    $dlg = [Windows.Forms.Form]::new()
    $dlg.Text = 'MamidLabs · Profiles'
    $dlg.Size = [Drawing.Size]::new(790, 500)
    $dlg.StartPosition = 'CenterParent'
    $dlg.FormBorderStyle = 'FixedDialog'
    $dlg.MaximizeBox = $false
    $dlg.MinimizeBox = $false
    $dlg.BackColor = $colPanel
    $dlg.ForeColor = $colText

    $list = [Windows.Forms.ListBox]::new()
    $list.SetBounds(16, 16, 230, 300)
    $list.BackColor = [Drawing.Color]::FromArgb(35, 39, 49)
    $list.ForeColor = $colText
    $list.BorderStyle = 'FixedSingle'

    $detail = [Windows.Forms.TextBox]::new()
    $detail.SetBounds(262, 16, 500, 300)
    $detail.Multiline = $true
    $detail.ReadOnly = $true
    $detail.ScrollBars = 'Vertical'
    $detail.WordWrap = $false
    $detail.BackColor = [Drawing.Color]::FromArgb(35, 39, 49)
    $detail.ForeColor = $colText
    $detail.BorderStyle = 'FixedSingle'

    $status = [Windows.Forms.Label]::new()
    $status.SetBounds(16, 326, 746, 40)

    $downloadBtn = [Windows.Forms.Button]::new()
    $downloadBtn.Text = 'Download missing'
    $downloadBtn.SetBounds(16, 376, 210, 34)
    $switchBtn = [Windows.Forms.Button]::new()
    $switchBtn.Text = 'Switch to this profile'
    $switchBtn.SetBounds(240, 376, 170, 34)
    $updateBtn = [Windows.Forms.Button]::new()
    $updateBtn.Text = 'Check for updates'
    $updateBtn.SetBounds(416, 376, 214, 34)
    $updateBtn.Tag = @{ state = 'idle' }
    $closeBtn = [Windows.Forms.Button]::new()
    $closeBtn.Text = 'Close'
    $closeBtn.SetBounds(642, 376, 120, 34)

    foreach ($profile in $catalog) { [void]$list.Items.Add("$($profile.label) — $(Get-ProfileStatusLabel $profile.id)") }

    $refresh = {
        $index = $list.SelectedIndex
        if ($index -lt 0) { return }
        $profile = $catalog[$index]
        $state = Get-ProfileState $profile.id
        $lines = [Collections.Generic.List[string]]::new()
        $lines.Add([string]$profile.label)
        $lines.Add('Hardware: ' + [string]$profile.hardware)
        $lines.Add('')
        $lines.Add([string]$profile.summary)
        $lines.Add('')
        foreach ($file in @($profile.files)) {
            $full = Join-Path $root ([string]$file.target)
            $mark = if (Test-Path -LiteralPath $full -PathType Leaf) { '[installed]' }
                    elseif ([string]$file.repo) { '[to download]' }
                    else { '[manual install]' }
            $lines.Add(('{0,-17}{1} ({2})' -f $mark, [string]$file.label, (Format-ByteSize ([long]$file.bytes))))
            $lines.Add('                 ' + [string]$file.target)
            if (!([string]$file.repo) -and [string]$file.source -and !(Test-Path -LiteralPath $full -PathType Leaf)) {
                $lines.Add('                 source: ' + [string]$file.source)
            }
        }
        if (@($state.missingManual).Count) {
            $lines.Add('')
            $lines.Add('[manual install] rows have no repository name in agent/profiles.json;')
            $lines.Add('they must be copied from an existing installation, downloaded from the source address')
            $lines.Add('and put in the location shown, or a source must be defined in the catalog.')
        }
        $detail.Lines = $lines.ToArray()
        $downloadable = @($state.missingDownloadable)
        if ($downloadable.Count) {
            $downloadBtn.Enabled = $true
            $downloadBtn.Text = "Download missing ($(Format-ByteSize (($downloadable | Measure-Object -Property bytes -Sum).Sum)))"
        } else {
            $downloadBtn.Enabled = $false
            $downloadBtn.Text = 'Nothing to download'
        }
    }
    $list.Add_SelectedIndexChanged($refresh)

    $downloadBtn.Add_Click({
        $index = $list.SelectedIndex
        if ($index -lt 0) { return }
        $failure = Start-ProfileDownload $catalog[$index].id
        if ($failure) { $status.Text = $failure; return }
        $dlg.Close()
    }.GetNewClosure())

    $switchBtn.Add_Click({
        $index = $list.SelectedIndex
        if ($index -lt 0) { return }
        $profile = $catalog[$index]
        $state = Get-ProfileState $profile.id
        # If the language model file is missing llama-server fails on startup; warn first.
        if (@($state.missingDownloadable).Count -or @($state.missingManual).Count) {
            $answer = [Windows.Forms.MessageBox]::Show(
                'Some files of this profile are missing. Switch anyway?',
                'Missing files', 'YesNo', 'Warning')
            if ($answer -ne 'Yes') { return }
        }
        if (Switch-ToProfile $profile.id) { $dlg.Close() }
    }.GetNewClosure())

    $closeBtn.Add_Click({ $dlg.Close() }.GetNewClosure())

    # The button has three states: idle "Check for updates" -> clicking it asks GitHub ->
    # if an update exists it turns into "Update now" (one more click downloads it and starts
    # the setup file); if not, it shows "Up to date" for a few seconds and then resets itself
    # with a Timer. The result is also written to the status line, because disabled button
    # text is hard to read on the dark theme. The network call follows the same pattern as
    # other windows' synchronous requests (e.g. api-settings.ps1's Verify button): it is short,
    # so no separate thread is needed.
    #
    # Once the setup wizard has started, the app closes itself (the main window's FormClosing
    # saves the chat and stops llama-server and ComfyUI). If it stayed open,
    # install-mamidlabs.ps1 would stop at the 'layout' stage with "MamidLabs is open" and the
    # update could never complete.
    $mainForm = $form
    $updateResetTimer = [Windows.Forms.Timer]::new()
    $updateResetTimer.Interval = 3000
    $updateResetTimer.Add_Tick({
        $updateResetTimer.Stop()
        $updateBtn.Tag = @{ state = 'idle' }
        $updateBtn.Text = 'Check for updates'
        $updateBtn.Enabled = $true
    }.GetNewClosure())
    $dlg.Add_FormClosed({ $updateResetTimer.Stop(); $updateResetTimer.Dispose() }.GetNewClosure())

    $updateBtn.Add_Click({
        $state = $updateBtn.Tag
        if ($state.state -eq 'available') {
            $updateBtn.Enabled = $false
            $updateBtn.Text = 'Downloading…'
            $dlg.Refresh()
            try {
                $downloadPath = Join-Path $env:TEMP "MamidLabs_Setup_$($state.version).exe"
                Invoke-WebRequest -Uri $state.downloadUrl -OutFile $downloadPath -UseBasicParsing -ErrorAction Stop
                if ($state.size -and (Get-Item -LiteralPath $downloadPath).Length -ne $state.size) {
                    throw 'The downloaded file size does not match.'
                }
                Start-Process -FilePath $downloadPath
                $status.Text = 'The setup wizard has started; MamidLabs is closing for the update.'
                $dlg.Close()
                if ($mainForm) { $mainForm.Close() }
            } catch {
                $updateBtn.Text = 'Download failed'
                $status.Text = 'The update could not be downloaded: ' + $_.Exception.Message
                $updateBtn.Tag = @{ state = 'idle' }
                $updateResetTimer.Start()
            }
            return
        }
        $updateResetTimer.Stop()
        $updateBtn.Enabled = $false
        $updateBtn.Text = 'Checking…'
        $dlg.Refresh()
        $result = Test-MamidLabsUpdateAvailable
        $localText = if ($result.localVersion) { $result.localVersion } else { 'unknown' }
        if ($result.available) {
            $updateBtn.Tag = @{ state = 'available'; downloadUrl = $result.downloadUrl; version = $result.version; size = $result.size }
            $updateBtn.Text = 'Update now'
            $updateBtn.Enabled = $true
            $status.Text = "A new version is available: $($result.version) (installed: $localText). Update now opens the setup wizard and closes MamidLabs."
        } else {
            $updateBtn.Tag = @{ state = 'idle' }
            $updateBtn.Text = if ($result.error) { $result.error } else { 'Up to date' }
            $status.Text = if ($result.error) { 'Could not check for updates: ' + $result.error } else { "MamidLabs is up to date (version $localText)." }
            $updateBtn.Enabled = $false
            $updateResetTimer.Start()
        }
    }.GetNewClosure())

    $dlg.Controls.AddRange(@($list, $detail, $status, $downloadBtn, $switchBtn, $updateBtn, $closeBtn))
    $wanted = 0
    if ($focusId) {
        for ($i = 0; $i -lt $catalog.Count; $i++) { if ($catalog[$i].id -eq $focusId) { $wanted = $i; break } }
    }
    $list.SelectedIndex = $wanted
    & $refresh
    [void]$dlg.ShowDialog($form)
    $dlg.Dispose()
}
