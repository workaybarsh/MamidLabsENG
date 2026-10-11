# llama-server alias per profile. Must stay identical to the $profileAliases table in
# scripts\manage-qwen.ps1; test_qwen_contracts checks this.
# The table is deliberately built inside a function: it is defined even when the file is
# loaded only for its functions, and no method is called on null.
function Get-LocalProfileAliases {
    if (!$script:localProfileAliases) {
        $script:localProfileAliases = @{ max = 'qwen3.8-27b-max'; flash = 'qwen3.8-27b-flash'; lite = 'qwen3.5-9b-lite' }
    }
    return $script:localProfileAliases
}

# Resolves the profile back from the alias the server reports. The health check and the
# restore after media jobs use this function.
function Get-ProfileFromAlias([string]$alias) {
    $aliases = Get-LocalProfileAliases
    foreach ($name in @('max', 'flash', 'lite')) {
        if ($alias -eq $aliases[$name]) { return $name }
    }
    if ($alias -like '*-lite') { return 'lite' }
    if ($alias -like '*-flash') { return 'flash' }
    return 'max'
}

# The fixed builtin profile definitions used both by the model bar and by the
# "Switch to this profile" flow; collected here so there is one source of truth.
function Get-BuiltinProfileDefinitions {
    return @(
        @{ id = 'builtin-max'; label = 'Qwen3.8 27B MAX'; profile = 'max' },
        @{ id = 'builtin-flash'; label = 'Qwen3.8 27B FLASH'; profile = 'flash' },
        @{ id = 'builtin-lite'; label = 'Qwen3.5 9B LITE'; profile = 'lite' }
    )
}

function Update-ProviderSelection {
    $script:suppressModelChoice=$true
    try {
        $modelChoice.Items.Clear()
        # Lite uses a separate 9B model for 8 GB VRAM / 16 GB RAM machines. 32K context
        # measured on a real machine: ~7500 MiB VRAM with the default Q5_K_M (on an 8 GB card
        # ~450-500 MiB stays free - tight but usable).
        # Only a profile whose language model files are on disk is listed (ComfyUI media
        # models excluded - the install may have used -SkipMedia and they are not needed for
        # chat); otherwise LITE (or MAX/FLASH) could be selected on a PC where it was never
        # installed, and manage-qwen.ps1 would fail with a missing-file error on selection.
        $script:modelEntries=@()
        foreach ($def in (Get-BuiltinProfileDefinitions)) {
            if (Test-LocalProfileChatFilesPresent $def.profile) {
                $script:modelEntries += @{ id = $def.id; provider = 'local'; label = $def.label; profile = $def.profile; contextLimit = 32768 }
            }
        }
        foreach($local in @($script:apiSettings.localModels)){if($local){$script:modelEntries+=@{id=$local.id;provider='local';label=$local.name;path=$local.path;profile='flash';contextLimit=8192}}}
        $script:modelEntries+=@{id='off';provider='local';label='Off';profile='off';contextLimit=0}
        foreach($id in (Get-ChatProviderIds)){
            $p=$script:apiSettings.providers[$id]
            foreach($name in @(@($p.models)+@($p.model) | Where-Object {$_} | Select-Object -Unique)){
                $script:modelEntries+=@{id="$id|$name";provider=$id;label="$id · $name";model=$name;contextLimit=$(if($p.contextLimit){[int]$p.contextLimit}else{32768})}
            }
        }
        foreach($entry in $script:modelEntries){[void]$modelChoice.Items.Add($entry.label)}
        $wanted=[string]$script:apiSettings.selectedModel;$index=-1
        for($i=0;$i -lt $script:modelEntries.Count;$i++){if($script:modelEntries[$i].id -eq $wanted){$index=$i;break}}
        if($index -lt 0){$index=0}
        $modelChoice.SelectedIndex=$index
        $entry=$script:modelEntries[$index]
        $script:providerId=$entry.provider;$script:apiSettings.selected=$entry.provider
        $script:apiSettings.selectedModel=$entry.id;$script:ctxLimit=$entry.contextLimit
        if($entry.provider -eq 'local'){
            $script:apiSettings.selectedLocalModel=if($entry.path){$entry.id}else{''}
            if($entry.profile -ne 'off'){$script:apiSettings.localProfile=$entry.profile}
        }
        $script:compactAt=[int]($script:ctxLimit*.67)
    }finally{$script:suppressModelChoice=$false}
    $metric.Text="$($modelChoice.SelectedItem) selected."
    Update-LiveMetrics
}

function Get-SelectedLocalAlias {
    if ($script:providerId -ne 'local' -or $script:apiSettings.selectedModel -eq 'off') { return '' }
    if ($script:apiSettings.selectedLocalModel) { return [string]$script:apiSettings.selectedLocalModel }
    $aliases = Get-LocalProfileAliases
    $profile = [string]$script:apiSettings.localProfile
    if (!$profile -or !$aliases.ContainsKey($profile)) { $profile = 'max' }
    return [string]$aliases[$profile]
}

function Select-RegisteredModel {
    if($script:suppressModelChoice -or $modelChoice.SelectedIndex -lt 0){return}
    if($script:busy -or $script:imageBusy -or $script:pendingSend){Update-ProviderSelection;return}
    $entry=$script:modelEntries[$modelChoice.SelectedIndex]
    $script:apiSettings.selectedModel=$entry.id;$script:apiSettings.selected=$entry.provider
    if($entry.provider -eq 'local' -and $entry.profile -ne 'off'){
        $script:apiSettings.selectedLocalModel=if($entry.path){$entry.id}else{''}
        $script:apiSettings.localProfile=$entry.profile
    }
    $script:providerId=$entry.provider
    $script:ctxLimit=$entry.contextLimit;$script:compactAt=[int]($script:ctxLimit*.67)
    Save-ApiSettings;Update-LiveMetrics
    if($entry.profile -eq 'off'){Stop-LocalQwen}
    elseif($entry.provider -eq 'local'){Restart-LocalQwen $entry.profile}
    else{
        Stop-LocalQwen
        $metric.Text="$($entry.label) selected."
    }
}

# Adds GGUF language model files to the list. For split models only the first part,
# excluding image projectors (mmproj). The files are used in place, not copied.
function Register-LocalModelFiles($files) {
    $files=@($files | Where-Object {$_.Name -notmatch '(?i)mmproj|projector' -and ($_.Name -notmatch '-(\d{5})-of-\d{5}' -or [int]$Matches[1] -eq 1)})
    $registered=@($script:apiSettings.localModels | Where-Object {$_})
    foreach($file in $files){if(!@($registered | Where-Object {$_.path -eq $file.FullName}).Count){$registered+=@{id=('local-'+[guid]::NewGuid().ToString('N'));name=$file.BaseName;path=$file.FullName}}}
    $script:apiSettings.localModels=$registered
    return $files.Count
}

# Selection by file path: one or more .gguf files. If cancelled, a folder scan is
# offered (to add all models in a folder at once).
function Add-LocalModelFolder {
    if($script:busy -or $script:imageBusy){$metric.Text='Wait for the current operation to finish first.';return}
    $pick=[Windows.Forms.OpenFileDialog]::new();$pick.Title='Choose a GGUF language model file';$pick.Filter='GGUF language model|*.gguf';$pick.Multiselect=$true
    try{
        if($pick.ShowDialog($form) -eq 'OK'){
            $files=@($pick.FileNames | ForEach-Object { Get-Item -LiteralPath $_ })
        } else {
            $folder=[Windows.Forms.FolderBrowserDialog]::new();$folder.Description='Or choose a GGUF language model folder (all are added)'
            try{
                if($folder.ShowDialog($form) -ne 'OK'){return}
                $files=@(Get-ChildItem -LiteralPath $folder.SelectedPath -Filter '*.gguf' -File -Recurse)
            }finally{$folder.Dispose()}
        }
        $count=Register-LocalModelFiles $files
        if(!$count){$metric.Text='No supported GGUF language model found (an image projector cannot be added on its own).';return}
        Save-ApiSettings;Update-ProviderSelection
        $metric.Text="$count GGUF models in the list. You can pick one at the bottom right."
    }finally{$pick.Dispose()}
}
