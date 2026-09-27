function Show-MediaSettings {
    if ($script:busy -or $script:imageBusy) { $metric.Text='Wait for the current operation to finish first.'; return }
    $dlg=[Windows.Forms.Form]::new();$dlg.Text='MamidLabs · Image and video models';$dlg.Size=[Drawing.Size]::new(650,480)
    $dlg.StartPosition='CenterParent';$dlg.FormBorderStyle='FixedDialog';$dlg.MaximizeBox=$false;$dlg.MinimizeBox=$false
    $dlg.BackColor=$colPanel;$dlg.ForeColor=$colText
    $fields=@{}
    $y=20
    foreach($kind in @('image','video')) {
        $label=[Windows.Forms.Label]::new();$label.Text=if($kind -eq 'image'){'Image generation'}else{'Video generation'};$label.SetBounds(20,$y,520,24)
        $combo=[Windows.Forms.ComboBox]::new();$combo.DropDownStyle='DropDownList';$combo.SetBounds(20,($y+28),150,28)
        [void]$combo.Items.AddRange(@('Local','OpenAI','Gemini'))
        $model=[Windows.Forms.ComboBox]::new();$model.DropDownStyle='DropDown';$model.SetBounds(185,($y+28),425,28)
        $combo.Tag=@{kind=$kind;model=$model}
        $combo.Add_SelectedIndexChanged({
            if($this.SelectedIndex -lt 0){return}
            $id=@('local','openai','gemini')[$this.SelectedIndex]
            $this.Tag.model.Items.Clear()
            $p=$script:apiSettings.providers[$id]
            if($id -eq 'local'){
                # Krea-2's full-precision weights live under diffusion_models;
                # Lite's GGUF quantized version lives under unet, where the
                # ComfyUI-GGUF custom node looks for it.
                $dirs=if($this.Tag.kind -eq 'image'){@('ComfyUI\models\diffusion_models','ComfyUI\models\unet')}else{@('ComfyUI\models\diffusion_models')}
                $rx=if($this.Tag.kind -eq 'image'){'(?i)krea2.*turbo'}else{'(?i)wan2[._]2.*ti2v.*5b'}
                $files=@()
                foreach($dir in $dirs){
                    $base=Join-Path $root $dir
                    if(!(Test-Path -LiteralPath $base)){continue}
                    $files+=@(Get-ChildItem -LiteralPath $base -Filter '*.safetensors' -File -Recurse | Where-Object {$_.Name -match $rx} | ForEach-Object {$_.FullName.Substring($base.Length+1).Replace('\','/')})
                    $files+=@(Get-ChildItem -LiteralPath $base -Filter '*.gguf' -File -Recurse | Where-Object {$_.Name -match $rx} | ForEach-Object {$_.FullName.Substring($base.Length+1).Replace('\','/')})
                }
                # External files added without copying are listed too (media-paths.ps1).
                foreach($external in @($script:apiSettings.externalMediaModels | Where-Object {$_ -and [string]$_.kind -eq $this.Tag.kind})){
                    if(Test-Path -LiteralPath ([string]$external.path) -PathType Leaf){$files+=[IO.Path]::GetFileName([string]$external.path)}
                }
                $files=@($files | Select-Object -Unique)
                foreach($file in $files){[void]$this.Tag.model.Items.Add($file)}
                $saved=[string]$script:apiSettings[($this.Tag.kind+'LocalModel')]
                $this.Tag.model.Text=if($saved){$saved}elseif($files){$files[0]}else{''}
            }else{
                if($p){foreach($name in @($p[($this.Tag.kind+'Models')])){if($name){[void]$this.Tag.model.Items.Add($name)}}}
                $this.Tag.model.Text=if($p){[string]$p[($this.Tag.kind+'Model')]}else{''}
            }
        })
        $id=[string]$script:apiSettings[($kind+'Provider')];$index=[Array]::IndexOf(@('local','openai','gemini'),$id)
        $combo.SelectedIndex=[math]::Max(0,$index)
        $fields[$kind]=@{combo=$combo;model=$model}
        $dlg.Controls.AddRange(@($label,$combo,$model));$y+=85
    }
    $note=[Windows.Forms.Label]::new();$note.SetBounds(20,225,590,110)
    $note.Text="For an API enter the full model id; save the keys in the API connections section. Locally the Krea 2 Turbo image and Wan 2.2 TI2V 5B video models are supported; other architectures need an extra pipeline. For images MAX/FLASH use the fp8_scaled (.safetensors) version of Krea 2 Turbo, Lite uses the GGUF quantized (Q4_K_M, .gguf) version. Video is generated with the same Wan 2.2 TI2V 5B file on all profiles; Lite is limited to 480p.`r`nClaude supports chat. The OpenAI Sora API shut down on 24 September 2026; the OpenAI video option may no longer respond. All choices are kept when the program is reopened."
    $browse=[Windows.Forms.Button]::new();$browse.Text='Choose model file (file path)…';$browse.SetBounds(20,188,340,30)
    $browse.Tag=$fields
    $browse.Add_Click({
        $pick=[Windows.Forms.OpenFileDialog]::new();$pick.Title='Krea 2 Turbo (.safetensors/.gguf) or Wan 2.2 TI2V 5B model file';$pick.Filter='Model|*.safetensors;*.gguf'
        try {
            if($pick.ShowDialog() -ne 'OK'){return}
            $kind=if(Test-MediaModelFile $pick.FileName 'image'){'image'}elseif(Test-MediaModelFile $pick.FileName 'video'){'video'}else{''}
            if(!$kind){[void][Windows.Forms.MessageBox]::Show('This window supports Krea 2 Turbo (.safetensors/.gguf) or Wan 2.2 TI2V 5B (.safetensors) weights.');return}
            # Default: the file is used in place (no copy). No: copied into the ComfyUI folder as before.
            $answer=[Windows.Forms.MessageBox]::Show("Use the file where it is?`r`n`r`nYes: not copied, only its path is saved (recommended).`r`nNo: copied into the MamidLabs ComfyUI folder.",'Model file','YesNoCancel','Question')
            if($answer -eq 'Cancel'){return}
            $name=[IO.Path]::GetFileName($pick.FileName)
            if($answer -eq 'Yes'){
                $script:apiSettings.externalMediaModels=@(Add-ExternalMediaModel $script:apiSettings.externalMediaModels $pick.FileName $kind)
                [void](Write-ComfyExtraModelPaths $root $script:apiSettings.externalMediaModels)
            } else {
                # GGUF (Lite) is looked for under unet by the ComfyUI-GGUF custom node.
                $folder='ComfyUI\models\'+(Get-ComfyModelFolder $pick.FileName)
                New-Item -ItemType Directory -Path (Join-Path $root $folder) -Force | Out-Null
                $target=Join-Path (Join-Path $root $folder) $name
                if(!(Test-Path -LiteralPath $target)){Copy-Item -LiteralPath $pick.FileName -Destination $target}
            }
            $script:apiSettings[($kind+'LocalModel')]=$name
            $script:apiSettings[($kind+'Provider')]='local'
            Save-ApiSettings
            # Switches the matching row to Local and refreshes the list; the new file comes selected.
            $field=$this.Tag[$kind];$field.combo.SelectedIndex=-1;$field.combo.SelectedIndex=0
            $field.model.Text=$name
            # After choosing there was no visible confirmation in the window; the user could not tell whether it worked.
            $label=if($kind -eq 'image'){'Image'}else{'Video'}
            $how=if($answer -eq 'Yes'){'will be used where it is, not copied'}else{'copied into the ComfyUI folder'}
            $this.Tag['status'].Text="$label model added: $name ($how)."
        } catch { [void][Windows.Forms.MessageBox]::Show('Could not add the model: '+$_.Exception.Message) }
        finally { $pick.Dispose() }
    });$dlg.Controls.Add($browse)
    $status=[Windows.Forms.Label]::new();$status.SetBounds(20,365,455,40)
    $fields['status']=$status
    $save=[Windows.Forms.Button]::new();$save.Text='Save';$save.SetBounds(500,365,110,32);$save.Tag=@{fields=$fields;status=$status}
    $save.Add_Click({
        try {
            foreach($kind in @('image','video')) {
                $field=$this.Tag.fields[$kind];$id=@('local','openai','gemini')[$field.combo.SelectedIndex]
                if($id -ne 'local' -and !$field.model.Text.Trim()){throw 'Enter the model id for the selected API.'}
            }
            foreach($kind in @('image','video')) {
                $field=$this.Tag.fields[$kind];$id=@('local','openai','gemini')[$field.combo.SelectedIndex]
                $script:apiSettings[($kind+'Provider')]=$id
                if($id -eq 'local'){$script:apiSettings[($kind+'LocalModel')]=$field.model.Text.Trim()}
                if($id -ne 'local') {
                    if(!$script:apiSettings.providers[$id]){$script:apiSettings.providers[$id]=@{}}
                    $script:apiSettings.providers[$id][($kind+'Model')]=$field.model.Text.Trim()
                    $p=$script:apiSettings.providers[$id]
                    $p[($kind+'Models')]=@(@($p[($kind+'Models')])+@($field.model.Text.Trim()) | Where-Object {$_} | Select-Object -Unique)
                }
            }
            Save-ApiSettings;$this.Tag.status.Text='Image and video choices saved.'
        }catch{$this.Tag.status.Text=$_.Exception.Message}
    })
    $dlg.Controls.AddRange(@($note,$status,$save));[void]$dlg.ShowDialog($form);$dlg.Dispose()
}

function Start-CloudMedia([string]$kind,[hashtable]$data) {
    $provider=[string]$script:apiSettings[($kind+'Provider')]
    if(!$provider -or $provider -eq 'local'){return $false}
    try {
        $data.kind=$kind;$data.provider=$provider;$data.root=$root
        $data.model=[string]$script:apiSettings.providers[$provider][($kind+'Model')]
        $data.apiKey=Get-ProviderKey $provider
        if(!$data.model -or !$data.apiKey){throw 'Enter the key in the API connections section and the model id in the Image and video API section.'}
        $script:sharedMedia=[hashtable]::Synchronized(@{Cancel=$false;Process=$null})
        $job=Start-ThreadJob -FilePath (Join-Path $root 'agent\api-media-run.ps1') -ArgumentList $data,$script:sharedMedia
        if($kind -eq 'image'){$script:imageJob=$job}else{$script:videoJob=$job}
        $script:imageBusy=$true;$send.Enabled=$true;$send.Text='Stop'
        $metric.Text="$provider API: sending the generation request…"
    }catch{Render-JobEvent ([pscustomobject]@{kind='error';text=$_.Exception.Message})}
    return $true
}
