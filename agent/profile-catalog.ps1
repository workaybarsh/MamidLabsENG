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

# From a profile's TARGETS removes the ones shared with other profiles; deletion removes
# only these unique files so another profile's installation is not broken. Example: MAX
# and FLASH use exactly the same file set (treated as aliases, not sharing); LITE shares
# part of its media files with MAX/FLASH.
function Get-ProfileUniqueTargets([string]$id) {
    $catalog = @(Get-ProfileCatalog)
    $entry = @($catalog | Where-Object { $_.id -eq $id })[0]
    if (!$entry) { return @() }
    $myTargets = @($entry.files | ForEach-Object { [string]$_.target })
    $mySignature = ($myTargets | Sort-Object -Unique) -join '|'
    $shared = @{}
    foreach ($profile in $catalog) {
        if ($profile.id -eq $id) { continue }
        $otherTargets = @($profile.files | ForEach-Object { [string]$_.target })
        # Same file set = an alias of the same profile (MAX/FLASH); not counted as sharing.
        if ((($otherTargets | Sort-Object -Unique) -join '|') -eq $mySignature) { continue }
        foreach ($target in $otherTargets) { $shared[$target] = $true }
    }
    $unique = [Collections.Generic.List[string]]::new()
    $seen = @{}
    foreach ($target in $myTargets) {
        if ($seen.ContainsKey($target)) { continue }
        $seen[$target] = $true
        if (!$shared.ContainsKey($target)) { [void]$unique.Add($target) }
    }
    return $unique.ToArray()
}

# Delete a profile: removes only its unique files, cleans up emptied folders and returns
# the removed/failed counts. The caller (Profile manager) takes confirmation.
function Remove-ProfileFiles([string]$id) {
    $result = @{ removed = 0; failed = 0; totalBytes = 0L }
    foreach ($target in (Get-ProfileUniqueTargets $id)) {
        $full = Join-Path $root $target
        if (!(Test-Path -LiteralPath $full -PathType Leaf)) { continue }
        try { $result.totalBytes += [long](Get-Item -LiteralPath $full).Length } catch { }
        try { Remove-Item -LiteralPath $full -Force -ErrorAction Stop; $result.removed++ }
        catch { $result.failed++ }
    }
    # Remove folders left empty (only the folders of this profile's targets).
    foreach ($target in (Get-ProfileUniqueTargets $id)) {
        $dir = Split-Path -Parent (Join-Path $root $target)
        try {
            if ((Test-Path -LiteralPath $dir -PathType Container) -and
                !(Get-ChildItem -LiteralPath $dir -Force -ErrorAction SilentlyContinue)) {
                Remove-Item -LiteralPath $dir -Force -ErrorAction SilentlyContinue
            }
        } catch { }
    }
    return $result
}

# Component classification: media (image/video) lives under ComfyUI, audio under
# venv\asr and whisper model files; everything else (language model/mmproj) is core.
function Get-ProfileFileComponent([string]$target) {
    $t = ([string]$target) -replace '\\', '/'
    if ($t -like 'ComfyUI/*') { return 'media' }
    if ($t -like 'venv/asr/*' -or $t -like 'models/whisper*') { return 'audio' }
    return 'core'
}

# Deletes the selected components' (media/audio) files for a profile; only touches files
# unique to this profile. Returns removed count and total bytes.
function Remove-ProfileComponents([string]$id, [string[]]$components) {
    $result = @{ removed = 0; failed = 0; totalBytes = 0L }
    foreach ($target in (Get-ProfileUniqueTargets $id)) {
        if ($components -notcontains (Get-ProfileFileComponent $target)) { continue }
        $full = Join-Path $root $target
        if (!(Test-Path -LiteralPath $full -PathType Leaf)) { continue }
        try { $result.totalBytes += [long](Get-Item -LiteralPath $full).Length } catch { }
        try { Remove-Item -LiteralPath $full -Force -ErrorAction Stop; $result.removed++ }
        catch { $result.failed++ }
    }
    return $result
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
# This edition's UI language and the "language profile" (other-language edition) repository.
# The Profiles-window language selector downloads and installs the other-language setup
# (installed into the same folder).
$script:MamidLabsEditionLang = 'en'
$script:OtherEditionRepo = 'workaybarsh/MamidLabsTR'

# A private repository needs a GitHub token for the update check and download; otherwise
# GitHub returns 404. The token is looked up in order: the GITHUB_TOKEN / GH_TOKEN environment
# variable, then the stored 'github' provider key (Windows DPAPI, same as the API settings).
function Get-UpdateToken {
    foreach ($envName in @('GITHUB_TOKEN', 'GH_TOKEN')) {
        $value = [Environment]::GetEnvironmentVariable($envName)
        if ($value) { return $value }
    }
    $profile = $null
    if ($script:apiSettings -and $script:apiSettings.providers) {
        $profile = $script:apiSettings.providers['github']
    }
    if ($profile -and $profile.key) {
        try {
            $secure = ConvertTo-SecureString ([string]$profile.key)
            return [Net.NetworkCredential]::new('', $secure).Password
        } catch { }
    }
    return ''
}

function Get-GitHubHeaders([string]$token) {
    $headers = @{ 'User-Agent' = 'MamidLabs-Updater'; 'Accept' = 'application/vnd.github+json' }
    if ($token) { $headers['Authorization'] = "Bearer $token" }
    return $headers
}

# The update check reads the local version from the uninstall key. Primary source: Inno
# Setup's own key ({AppId}_is1, a wizard install). Fallback: a script-based install writes
# its own entry (MamidLabs-<hash8>) with InstallLocation = the install root; it is matched
# by InstallLocation/DisplayName. Without the fallback a script-installed copy looked like
# it had no local version and the check assumed an update every time.
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
    if (!$root) { return $null }
    $normalized = [IO.Path]::GetFullPath($root).TrimEnd('\')
    foreach ($hive in @('HKCU:', 'HKLM:')) {
        $base = "$hive\Software\Microsoft\Windows\CurrentVersion\Uninstall"
        if (!(Test-Path -LiteralPath $base)) { continue }
        foreach ($key in @(Get-ChildItem -LiteralPath $base -ErrorAction SilentlyContinue)) {
            $props = Get-ItemProperty -LiteralPath $key.PSPath -ErrorAction SilentlyContinue
            if (!$props -or [string]$props.DisplayName -notlike 'MamidLabs*') { continue }
            if (([string]$props.InstallLocation).TrimEnd('\') -ine $normalized) { continue }
            if ($props.DisplayVersion) { return [string]$props.DisplayVersion }
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
    $token = Get-UpdateToken
    try {
        $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$($script:MamidLabsRepo)/releases/latest" `
            -Headers (Get-GitHubHeaders $token) -TimeoutSec 20 -ErrorAction Stop
    } catch {
        $reason = if (!$token) { 'an update token is required for the private repo' } else { 'no internet?' }
        return @{ available = $false; error = "Could not check ($reason)"; localVersion = $local }
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
    return @{ available = $isNewer; version = $remoteVersion; downloadUrl = [string]$asset.browser_download_url
              apiUrl = [string]$asset.url; size = [long]$asset.size; localVersion = $local }
}

# Finds the latest setup package of the other-language edition (language-profile download).
# Result: @{ version; downloadUrl; apiUrl; size } or @{ error }. Never throws on a network error.
function Get-EditionRelease([string]$repo) {
    $token = Get-UpdateToken
    try {
        $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/latest" `
            -Headers (Get-GitHubHeaders $token) -TimeoutSec 20 -ErrorAction Stop
    } catch {
        $reason = if (!$token) { 'an update token is required for a private repository' } else { 'no internet?' }
        return @{ error = "Could not check ($reason)" }
    }
    $asset = @($release.assets | Where-Object { $_.name -like '*Setup*.exe' }) | Select-Object -First 1
    if (!$asset) { return @{ error = 'Setup file not found' } }
    return @{ version = ([string]$release.tag_name).TrimStart('v', 'V')
              downloadUrl = [string]$asset.browser_download_url
              apiUrl = [string]$asset.url; size = [long]$asset.size }
}

# Persistent path and record for the downloaded language profile (other-language edition)
# package. Kept so the user can delete it later (a persistent folder, not %TEMP%).
function Get-EditionDownloadDir { return (Join-Path $root 'tools\editions') }
function Get-EditionRecordPath { return (Join-Path (Get-EditionDownloadDir) 'edition.json') }

function Get-EditionRecord {
    $path = Get-EditionRecordPath
    if (!(Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
    try { return (Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json) } catch { return $null }
}

# The Profiles-window handlers are closures (GetNewClosure) and CANNOT see `$script:`
# variables (inside a closure `$script:` points at the closure's own module scope and comes
# back empty). The "busy" flags are live values, so they cannot be copied into a local (it
# would go stale); the live value is read through this function instead (inside a function
# `$script:` refers to the real script scope).
function Test-ProfileManagerBusy { return [bool]($script:busy -or $script:imageBusy) }

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

function Start-ProfileDownload([string]$id, [string[]]$components = @('core', 'media', 'audio')) {
    if ($script:busy -or $script:imageBusy) { return 'Wait for the current operation to finish first.' }
    $state = Get-ProfileState $id
    $missing = @($state.missingDownloadable | Where-Object { $components -contains (Get-ProfileFileComponent ([string]$_.target)) })
    if (!$missing.Count) { return 'There are no files to download for the selected components.' }
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
    $dlg.Size = [Drawing.Size]::new(790, 660)
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
    $downloadBtn.SetBounds(16, 376, 180, 34)
    $switchBtn = [Windows.Forms.Button]::new()
    $switchBtn.Text = 'Switch to this profile'
    $switchBtn.SetBounds(202, 376, 150, 34)
    $deleteBtn = [Windows.Forms.Button]::new()
    $deleteBtn.Text = 'Delete profile'
    $deleteBtn.SetBounds(358, 376, 120, 34)
    $updateBtn = [Windows.Forms.Button]::new()
    $updateBtn.Text = 'Check for updates'
    $updateBtn.SetBounds(484, 376, 180, 34)
    $updateBtn.Tag = @{ state = 'idle' }
    $closeBtn = [Windows.Forms.Button]::new()
    $closeBtn.Text = 'Close'
    $closeBtn.SetBounds(670, 376, 104, 34)

    # A private repository needs a token for the update check/download (see Get-UpdateToken).
    $tokenLabel = [Windows.Forms.Label]::new()
    $tokenLabel.Text = 'Update token (GitHub token for the private repo):'
    $tokenLabel.SetBounds(16, 418, 420, 20)
    $tokenBox = [Windows.Forms.TextBox]::new()
    $tokenBox.SetBounds(16, 440, 430, 26)
    $tokenBox.UseSystemPasswordChar = $true
    $tokenSave = [Windows.Forms.Button]::new()
    $tokenSave.Text = 'Save token'
    $tokenSave.SetBounds(456, 439, 130, 28)
    $tokenClear = [Windows.Forms.Button]::new()
    $tokenClear.Text = 'Clear'
    $tokenClear.SetBounds(592, 439, 60, 28)
    # Component selection: optional components to download/delete (language model is core).
    $mediaChk = [Windows.Forms.CheckBox]::new()
    $mediaChk.Text = 'Image/video generation'
    $mediaChk.SetBounds(16, 478, 200, 24)
    $mediaChk.Checked = $true
    $mediaChk.ForeColor = $colText
    $audioChk = [Windows.Forms.CheckBox]::new()
    $audioChk.Text = 'Audio capture'
    $audioChk.SetBounds(224, 478, 200, 24)
    $audioChk.Checked = $true
    $audioChk.ForeColor = $colText
    $delCompBtn = [Windows.Forms.Button]::new()
    $delCompBtn.Text = 'Delete selected components'
    $delCompBtn.SetBounds(16, 508, 200, 34)
    # The language-profile values are NOT visible through $script: inside GetNewClosure() (the
    # closure looks at its own module scope, so $script: comes back empty); capture them into
    # locals instead. Otherwise the "already installed" check and the other-edition repo are
    # empty (see $mainForm = $form, pitfall 21).
    $editionLang = $script:MamidLabsEditionLang
    $otherEditionRepo = $script:OtherEditionRepo
    # Language profile: this edition's language is selected by default; the user can download
    # and install the other-language edition (Turkish/English) here. Installed into the same folder.
    $langLabel = [Windows.Forms.Label]::new()
    $langLabel.Text = 'UI language / language profile:'
    $langLabel.SetBounds(16, 552, 210, 20)
    $langLabel.ForeColor = $colText
    $langCombo = [Windows.Forms.ComboBox]::new()
    $langCombo.DropDownStyle = 'DropDownList'
    [void]$langCombo.Items.Add('Türkçe')
    [void]$langCombo.Items.Add('English')
    $langCombo.SetBounds(232, 550, 140, 26)
    $langCombo.SelectedIndex = if ($editionLang -eq 'en') { 1 } else { 0 }
    $langBtn = [Windows.Forms.Button]::new()
    $langBtn.Text = 'Download selected language edition'
    $langBtn.SetBounds(380, 549, 240, 28)
    $langDeleteBtn = [Windows.Forms.Button]::new()
    $langDeleteBtn.Text = 'Delete language edition'
    $langDeleteBtn.SetBounds(626, 549, 136, 28)
    if ($script:apiSettings.providers -and $script:apiSettings.providers['github']) {
        $tokenLabel.Text = 'Update token saved (leave blank to keep it; type to replace):'
    }

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
        $components = @('core')
        if ($mediaChk.Checked) { $components += 'media' }
        if ($audioChk.Checked) { $components += 'audio' }
        $failure = Start-ProfileDownload $catalog[$index].id $components
        if ($failure) { $status.Text = $failure; return }
        $dlg.Close()
    }.GetNewClosure())

    # Delete the selected components (image/video, audio) from this profile: only files
    # unique to this profile are removed; core (language model) and shared files are kept.
    $delCompBtn.Add_Click({
        $index = $list.SelectedIndex
        if ($index -lt 0) { return }
        if (Test-ProfileManagerBusy) { $status.Text = 'Wait for the current operation to finish first.'; return }
        $components = @()
        if ($mediaChk.Checked) { $components += 'media' }
        if ($audioChk.Checked) { $components += 'audio' }
        if (!$components.Count) { $status.Text = 'No component selected (Image/video or Audio).'; return }
        $profile = $catalog[$index]
        $answer = [Windows.Forms.MessageBox]::Show(
            ('The selected components for ' + $profile.label + ' will be deleted (only files unique to this profile).' + "`r`n`r`nDelete?"),
            'Delete components', 'YesNo', 'Warning')
        if ($answer -ne 'Yes') { return }
        $result = Remove-ProfileComponents $profile.id $components
        $list.Items[$index] = "$($profile.label) — $(Get-ProfileStatusLabel $profile.id)"
        & $refresh
        $status.Text = if ($result.removed) { "Selected components deleted ($($result.removed) files, $(Format-ByteSize $result.totalBytes))." }
            else { 'No (profile-unique) files found to delete.' }
        try { Update-ProviderSelection } catch { }
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

    # Delete profile: removes only files UNIQUE to this profile; files shared with other
    # profiles (e.g. LITE's media files also used by MAX/FLASH) are kept. If the active
    # profile is deleted, the model is stopped first.
    # $script:apiSettings is invisible inside a closure ($script: comes back empty), so it is
    # captured into a local; it is the same object, so token save/clear also mutates the real
    # settings.
    $settings = $script:apiSettings
    $deleteBtn.Add_Click({
        $index = $list.SelectedIndex
        if ($index -lt 0) { return }
        if (Test-ProfileManagerBusy) { $status.Text = 'Wait for the current job to finish first.'; return }
        $profile = $catalog[$index]
        $present = @((Get-ProfileUniqueTargets $profile.id) | Where-Object {
            Test-Path -LiteralPath (Join-Path $root $_) -PathType Leaf })
        if (!$present.Count) {
            $status.Text = 'Nothing to delete: the profile is not installed or all its files are shared with another profile.'
            return
        }
        $bytes = 0L
        foreach ($rel in $present) { try { $bytes += [long](Get-Item -LiteralPath (Join-Path $root $rel)).Length } catch { } }
        $isActive = ([string]$settings.selectedModel -eq "builtin-$($profile.id)")
        $warn = if ($isActive) { "`r`n`r`nWARNING: This profile is currently selected. The model is stopped first." } else { '' }
        $answer = [Windows.Forms.MessageBox]::Show(
            ($profile.label + ': ' + $present.Count + ' file(s) (' + (Format-ByteSize $bytes) + ') will be deleted.' +
             "`r`nFiles shared with other profiles are kept." + $warn + "`r`n`r`nDelete?"),
            'Delete profile', 'YesNo', 'Warning')
        if ($answer -ne 'Yes') { return }
        if ($isActive) { try { Stop-LocalQwen } catch { } }
        $result = Remove-ProfileFiles $profile.id
        $list.Items[$index] = "$($profile.label) — $(Get-ProfileStatusLabel $profile.id)"
        & $refresh
        $status.Text = if ($result.failed) { "$($result.failed) file(s) could not be deleted (possibly in use)." }
            else { "Profile files deleted ($($result.removed) file(s), $(Format-ByteSize $result.totalBytes)); shared files kept." }
        try { Update-ProviderSelection } catch { }
    }.GetNewClosure())

    # $settings is the real settings object captured above; the token save/clear closures
    # (GetNewClosure) use it because $script:apiSettings comes back empty inside a closure.
    $tokenSave.Add_Click({
        if (!$settings.providers) { $settings.providers = @{} }
        $value = $tokenBox.Text.Trim()
        if ($value) {
            $settings.providers['github'] = @{ key = (ConvertFrom-SecureString (ConvertTo-SecureString $value -AsPlainText -Force)) }
            $status.Text = 'Update token saved. Try "Check for updates".'
        } else {
            $status.Text = 'Empty token was not saved.'
        }
        $tokenBox.Text = ''
        Save-ApiSettings
    }.GetNewClosure())
    $tokenClear.Add_Click({
        if ($settings.providers -and $settings.providers.ContainsKey('github')) {
            $settings.providers.Remove('github')
        }
        Save-ApiSettings
        $status.Text = 'Update token cleared.'
    }.GetNewClosure())

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
                $token = Get-UpdateToken
                $dlgUrl = if ($token -and $state.apiUrl) { $state.apiUrl } else { $state.downloadUrl }
                $dlgHeaders = if ($token -and $state.apiUrl) {
                    @{ 'User-Agent' = 'MamidLabs-Updater'; 'Accept' = 'application/octet-stream'; 'Authorization' = "Bearer $token" }
                } else { @{ 'User-Agent' = 'MamidLabs-Updater' } }
                Invoke-WebRequest -Uri $dlgUrl -Headers $dlgHeaders -OutFile $downloadPath -UseBasicParsing -ErrorAction Stop
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
            $updateBtn.Tag = @{ state = 'available'; downloadUrl = $result.downloadUrl; apiUrl = $result.apiUrl; version = $result.version; size = $result.size }
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

    $langBtn.Add_Click({
        $chosen = if ($langCombo.SelectedIndex -eq 1) { 'en' } else { 'tr' }
        if ($chosen -eq $editionLang) {
            $status.Text = 'The selected language edition is already installed.'
            return
        }
        $langBtn.Enabled = $false
        $langBtn.Text = 'Downloading…'
        $dlg.Refresh()
        $release = Get-EditionRelease $otherEditionRepo
        if ($release.error) {
            $status.Text = 'Could not download the language edition: ' + $release.error
            $langBtn.Text = 'Download selected language edition'
            $langBtn.Enabled = $true
            return
        }
        try {
            # The package is kept persistently (tools\editions); "Delete" removes this file + record.
            $dir = Get-EditionDownloadDir
            New-Item -ItemType Directory -Path $dir -Force | Out-Null
            $downloadPath = Join-Path $dir ("MamidLabs_Setup_" + $chosen + ".exe")
            $token = Get-UpdateToken
            $url = if ($token -and $release.apiUrl) { $release.apiUrl } else { $release.downloadUrl }
            $headers = if ($token -and $release.apiUrl) {
                @{ 'User-Agent' = 'MamidLabs-Updater'; 'Accept' = 'application/octet-stream'; 'Authorization' = "Bearer $token" }
            } else { @{ 'User-Agent' = 'MamidLabs-Updater' } }
            Invoke-WebRequest -Uri $url -Headers $headers -OutFile $downloadPath -UseBasicParsing -ErrorAction Stop
            if ($release.size -and (Get-Item -LiteralPath $downloadPath).Length -ne $release.size) { throw 'Downloaded file size does not match.' }
            @{ lang = $chosen; version = [string]$release.version; path = $downloadPath; installedAt = (Get-Date).ToString('o') } |
                ConvertTo-Json | Set-Content -LiteralPath (Get-EditionRecordPath) -Encoding UTF8
            $langDeleteBtn.Enabled = $true
            Start-Process -FilePath $downloadPath
            $status.Text = 'The language-edition setup wizard opened; MamidLabs is closing.'
            $dlg.Close()
            if ($mainForm) { $mainForm.Close() }
        } catch {
            $status.Text = 'Could not download the language edition: ' + $_.Exception.Message
            $langBtn.Text = 'Download selected language edition'
            $langBtn.Enabled = $true
        }
    }.GetNewClosure())

    $langDeleteBtn.Add_Click({
        $record = Get-EditionRecord
        if (!$record) { $status.Text = 'There is no downloaded language edition to delete.'; return }
        try {
            if ($record.path -and (Test-Path -LiteralPath $record.path -PathType Leaf)) {
                Remove-Item -LiteralPath $record.path -Force
            }
            Remove-Item -LiteralPath (Get-EditionRecordPath) -Force -ErrorAction SilentlyContinue
            $langDeleteBtn.Enabled = $false
            $status.Text = 'The downloaded language-edition package was deleted.'
        } catch {
            $status.Text = 'Could not delete the language-edition package: ' + $_.Exception.Message
        }
    }.GetNewClosure())

    # The "Delete" button is enabled only when a downloaded language package exists.
    $langDeleteBtn.Enabled = [bool](Get-EditionRecord)

    $dlg.Controls.AddRange(@($list, $detail, $status, $downloadBtn, $switchBtn, $deleteBtn, $updateBtn, $closeBtn, $tokenLabel, $tokenBox, $tokenSave, $tokenClear, $mediaChk, $audioChk, $delCompBtn, $langLabel, $langCombo, $langBtn, $langDeleteBtn))
    $wanted = 0
    if ($focusId) {
        for ($i = 0; $i -lt $catalog.Count; $i++) { if ($catalog[$i].id -eq $focusId) { $wanted = $i; break } }
    }
    $list.SelectedIndex = $wanted
    & $refresh
    [void]$dlg.ShowDialog($form)
    $dlg.Dispose()
}
