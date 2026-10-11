# API keys are encrypted by Windows DPAPI for the current Windows account.
function Read-ApiSettings {
    $path = Join-Path $root 'settings\api.json'
    if (Test-Path -LiteralPath $path) {
        try { return (Get-Content -LiteralPath $path -Raw | ConvertFrom-Json -AsHashtable) } catch { }
    }
    return @{selected='local';providers=@{}}
}
function Save-ApiSettings {
    $dir = Join-Path $root 'settings'
    [void][IO.Directory]::CreateDirectory($dir)
    $path = Join-Path $dir 'api.json'
    $temp = $path + '.tmp'
    try {
        [IO.File]::WriteAllText($temp, ($script:apiSettings | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
        if ([IO.File]::Exists($path)) { [IO.File]::Replace($temp, $path, [NullString]::Value) }
        else { [IO.File]::Move($temp, $path) }
    } finally {
        if ([IO.File]::Exists($temp)) { Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue }
    }
}
function Get-ProviderKey([string]$provider) {
    $profile = $script:apiSettings.providers[$provider]
    if ($profile -and $profile.key) {
        try {
            $secure = ConvertTo-SecureString ([string]$profile.key)
            return [Net.NetworkCredential]::new('', $secure).Password
        } catch { throw 'The API key could not be decrypted on this Windows account. Enter it again in the settings.' }
    }
    $envName = if ($provider -eq 'huggingface') { 'HF_TOKEN' } else { [string](Get-ChatProvider $provider).env }
    if ($envName) { return [Environment]::GetEnvironmentVariable($envName) }
    return ''
}
function Show-ApiSettings {
    if ($script:busy -or $script:imageBusy) { $metric.Text='Wait for the current operation to finish before changing connection settings.'; return }
    $providers = @(Get-ChatProviders)
    $dlg = [Windows.Forms.Form]::new()
    $dlg.Text='MamidLabs · API connections'
    $dlg.Size=[Drawing.Size]::new(570,520)
    $dlg.StartPosition='CenterParent'; $dlg.FormBorderStyle='FixedDialog'; $dlg.MaximizeBox=$false; $dlg.MinimizeBox=$false
    $dlg.BackColor=$colPanel; $dlg.ForeColor=$colText
    $provider=[Windows.Forms.ComboBox]::new();$provider.DropDownStyle='DropDownList';$provider.SetBounds(20,42,510,28)
    [void]$provider.Items.AddRange(@($providers | ForEach-Object { [string]$_.label }))
    # Account linking: the key page opens in the browser, the pasted key is validated against the
    # provider's model list and the models the account can access fill the list.
    $openPage=[Windows.Forms.Button]::new();$openPage.Text='1. Sign in to your account, get a key';$openPage.SetBounds(20,108,250,30)
    # 'custom' has no ready-made account page; a base-address box is shown in the same
    # spot instead (see the $accountLabel.Text change below).
    $baseUrl=[Windows.Forms.TextBox]::new();$baseUrl.SetBounds(20,108,510,28);$baseUrl.Visible=$false
    $key=[Windows.Forms.TextBox]::new();$key.SetBounds(20,174,510,28);$key.UseSystemPasswordChar=$true
    $verify=[Windows.Forms.Button]::new();$verify.Text='2. Verify and fetch models';$verify.SetBounds(20,208,250,30)
    $model=[Windows.Forms.ComboBox]::new();$model.DropDownStyle='DropDown';$model.SetBounds(20,274,510,28)
    $context=[Windows.Forms.NumericUpDown]::new();$context.SetBounds(20,340,190,28);$context.Minimum=4096;$context.Maximum=2000000;$context.Increment=1024
    $accountLabel=$null;$keyLabel=$null
    foreach ($item in @(@('Provider',20),@('Account connection',86),@('API key — leave empty to keep the saved key',152),@('Model id (pick from the list or type the full API model name)',252),@('Context limit to use (tokens — auto-detected when the key is verified)',318))) {
        $label=[Windows.Forms.Label]::new();$label.Text=$item[0];$label.SetBounds(20,[int]$item[1],510,22);$dlg.Controls.Add($label)
        if ([int]$item[1] -eq 86) { $accountLabel = $label }
        if ([int]$item[1] -eq 152) { $keyLabel = $label }
    }
    $note=[Windows.Forms.Label]::new();$note.SetBounds(20,374,510,46)
    $note.Text='While an API is selected, the messages you send, attachments and tool results are forwarded to the selected provider. API fees are charged to your own account. The key is stored encrypted, bound to your Windows account.'
    $save=[Windows.Forms.Button]::new();$save.Text='Save';$save.SetBounds(420,430,110,32)
    $status=[Windows.Forms.Label]::new();$status.SetBounds(20,430,390,40)
    $dlg.Controls.AddRange(@($provider,$openPage,$baseUrl,$key,$verify,$model,$context,$note,$save,$status))
    $fields=@{provider=$provider;model=$model;key=$key;baseUrl=$baseUrl;context=$context;status=$status;providers=$providers;dialog=$dlg;openPage=$openPage;accountLabel=$accountLabel;keyLabel=$keyLabel;contexts=@{}}
    $provider.Tag=$fields;$save.Tag=$fields;$openPage.Tag=$fields;$verify.Tag=$fields;$model.Tag=$fields
    # When a model is selected/typed, apply the context window detected from the endpoint
    # ($f.contexts is filled once the key is verified).
    $model.Add_TextChanged({
        $f=$this.Tag;$name=$this.Text.Trim()
        if($f.contexts -and $f.contexts.ContainsKey($name)){$f.context.Value=[Math]::Min([int]$f.contexts[$name],[int]$f.context.Maximum)}
    })
    $provider.Add_SelectedIndexChanged({
        $f=$this.Tag;$id=[string]$f.providers[$this.SelectedIndex].id;$p=$script:apiSettings.providers[$id]
        $f.model.Items.Clear()
        foreach($name in @(@($p.models)+@($p.model) | Where-Object {$_} | Select-Object -Unique)){[void]$f.model.Items.Add($name)}
        $f.model.Text=if($p){[string]$p.model}else{''}
        $f.key.Text=''
        $f.context.Value=if($p.contextLimit){[int]$p.contextLimit}else{32768}
        $f.status.Text=if($p.key){'A saved key exists.'}else{'No key saved yet.'}
        # 'custom' has no ready-made account page; instead a box is shown where the user
        # types their own API address. Its key is also optional (most local servers -
        # Ollama, LM Studio, vLLM etc. - do not require authentication).
        if ($id -eq 'custom') {
            $f.openPage.Visible=$false;$f.baseUrl.Visible=$true
            $f.baseUrl.Text=if($p.baseUrl){[string]$p.baseUrl}else{''}
            $f.accountLabel.Text='Base address (e.g. http://127.0.0.1:1234/v1 — WITHOUT a trailing /chat/completions)'
            $f.keyLabel.Text='API key — optional, most local/own servers (Ollama, LM Studio, vLLM etc.) do not require one'
        } else {
            $f.openPage.Visible=$true;$f.baseUrl.Visible=$false
            $f.accountLabel.Text='Account connection'
            $f.keyLabel.Text='API key — leave empty to keep the saved key'
        }
    })
    $openPage.Add_Click({
        $f=$this.Tag;$entry=$f.providers[$f.provider.SelectedIndex]
        try { Start-Process ([string]$entry.keyPage); $f.status.Text="The $($entry.label) key page opened in the browser. Paste the key you created below." }
        catch { $f.status.Text='Could not open the browser: '+[string]$entry.keyPage }
    })
    $verify.Add_Click({
        $f=$this.Tag;$entry=$f.providers[$f.provider.SelectedIndex];$id=[string]$entry.id
        $candidate=$f.key.Text.Trim()
        $f.status.Text='Verifying…';$f.dialog.Cursor=[Windows.Forms.Cursors]::WaitCursor;$f.dialog.Refresh()
        try {
            if(!$candidate){$candidate=Get-ProviderKey $id}
            # For 'custom', try the not-yet-saved on-screen base address (see
            # Get-ProviderModels's $baseUrlOverride parameter); the same for the others.
            $names=if($id -eq 'custom'){@(Get-ProviderModels $id $candidate $f.baseUrl.Text.Trim())}else{@(Get-ProviderModels $id $candidate)}
            $current=$f.model.Text
            $f.model.Items.Clear();foreach($name in $names){[void]$f.model.Items.Add($name)}
            $f.model.Text=if($current -and $names -contains $current){$current}elseif($names){$names[0]}else{$current}
            # Auto-detect the context window from the endpoint (once a key is entered). If the
            # endpoint does not report context_length, the current value is kept.
            $f.contexts = if($id -eq 'custom'){ Get-ProviderModelContexts $id $candidate $f.baseUrl.Text.Trim() } else { Get-ProviderModelContexts $id $candidate }
            if(@($f.contexts.Keys).Count){
                $sel=$f.model.Text.Trim()
                $val = if($f.contexts.ContainsKey($sel)){[int]$f.contexts[$sel]}else{([int[]]@($f.contexts.Values)|Measure-Object -Maximum).Maximum}
                if($val -gt 0){ $f.context.Value=[Math]::Min($val,[int]$f.context.Maximum) }
                $f.status.Text="Key is valid: $($names.Count) models. Context auto-detected: $([int]$f.context.Value) tokens."
            } else {
                $f.status.Text="Key is valid: $($names.Count) models found. Pick a model and press Save."
            }
        } catch { $f.status.Text=$_.Exception.Message }
        finally { $f.dialog.Cursor=[Windows.Forms.Cursors]::Default }
    })
    $save.Add_Click({
        $fields=$this.Tag;$id=[string]$fields.providers[$fields.provider.SelectedIndex].id
        try {
            $p=$script:apiSettings.providers[$id]
            if (!$p) {$p=@{}}
            $updated=$p.Clone()
            $updated.model=$fields.model.Text.Trim();$updated.contextLimit=[int]$fields.context.Value
            # Persist ALL models fetched during verification, so the API's models also appear in the
            # model selection list (not just the selected one).
            $updated.models=@(@($fields.model.Items | Where-Object {$_})+@($p.models)+@($updated.model) | Where-Object {$_} | Select-Object -Unique)
            if ($id -eq 'custom') {
                $trimmed=$fields.baseUrl.Text.Trim().TrimEnd('/')
                if (!$trimmed) { throw 'First enter a base address (e.g. http://127.0.0.1:1234/v1).' }
                $updated.baseUrl=$trimmed
            }
            if ($fields.key.Text.Trim()) { $updated.key=ConvertFrom-SecureString (ConvertTo-SecureString $fields.key.Text.Trim() -AsPlainText -Force) }
            $script:apiSettings.providers[$id]=$updated
            Save-ApiSettings
            $fields.key.Text='';$fields.status.Text='Saved.'
            Update-ProviderSelection
        } catch { $fields.status.Text='Could not save: '+$_.Exception.Message }
    })
    $provider.SelectedIndex=0
    [void]$dlg.ShowDialog($form)
    $dlg.Dispose()
}
