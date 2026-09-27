function Show-HuggingFaceSettings {
    if($script:busy -or $script:imageBusy){$metric.Text='Wait for the current operation to finish first.';return}
    $dlg=[Windows.Forms.Form]::new();$dlg.Text='MamidLabs · Hugging Face';$dlg.Size=[Drawing.Size]::new(620,470)
    $dlg.StartPosition='CenterParent';$dlg.BackColor=$colPanel;$dlg.ForeColor=$colText;$dlg.FormBorderStyle='FixedDialog';$dlg.MaximizeBox=$false
    $fields=@{}
    $rows=@(@('token','Token (leave empty to keep the saved token)'),@('repo','Model repository (org/model)'),@('filename','File path inside the repository'),@('destination','Download folder'))
    $y=16
    foreach($row in $rows){
        $label=[Windows.Forms.Label]::new();$label.Text=$row[1];$label.SetBounds(20,$y,560,22)
        $box=[Windows.Forms.TextBox]::new();$box.SetBounds(20,($y+24),560,26);$fields[$row[0]]=$box
        $dlg.Controls.AddRange(@($label,$box));$y+=65
    }
    $fields.token.UseSystemPasswordChar=$true;$fields.destination.Text=Join-Path $root 'models\huggingface'
    $note=[Windows.Forms.Label]::new();$note.SetBounds(20,280,560,48);$note.Text='A read-access token is enough. Gated models require access permission on Hugging Face. The token is stored encrypted only on this Windows account; downloaded files are never run automatically.'
    $status=[Windows.Forms.Label]::new();$status.SetBounds(20,382,560,40)
    $dlg.Controls.AddRange(@($note,$status))
    foreach($action in @(@('save','Save',20),@('test','Test connection',155),@('download','Download file',300),@('remove','Remove token',445))){
        $btn=[Windows.Forms.Button]::new();$btn.Text=$action[1];$btn.SetBounds([int]$action[2],338,130,32)
        $btn.Tag=@{action=$action[0];fields=$fields;status=$status;dialog=$dlg}
        $btn.Add_Click({
            try{
                $f=$this.Tag.fields;$action=$this.Tag.action
                if($action -eq 'remove'){$script:apiSettings.providers.Remove('huggingface');Save-ApiSettings;$f.token.Text='';$this.Tag.status.Text='The saved token was removed. The HF_TOKEN environment variable can be used if set.';return}
                if($f.token.Text.Trim()){
                    $script:apiSettings.providers.huggingface=@{key=(ConvertFrom-SecureString (ConvertTo-SecureString $f.token.Text.Trim() -AsPlainText -Force))}
                    Save-ApiSettings;$f.token.Text=''
                }
                $token=Get-ProviderKey 'huggingface'
                if(!$token){throw 'Enter a token.'}
                if($action -eq 'save'){$this.Tag.status.Text='Token saved encrypted.';return}
                if($action -eq 'download' -and (!$f.repo.Text.Trim() -or !$f.filename.Text.Trim() -or !$f.destination.Text.Trim())){throw 'Fill in the repository, file path and destination folder.'}
                $data=@{kind='huggingface';apiKey=$token;action=$action;repo=$f.repo.Text.Trim();filename=$f.filename.Text.Trim();destination=$f.destination.Text.Trim();root=$root}
                $script:activeBubble=New-Bubble 'assistant';$script:activeBubble.body.Text='Hugging Face operation…'
                $script:sharedMedia=[hashtable]::Synchronized(@{Cancel=$false;Process=$null})
                $script:imageJob=Start-ThreadJob -FilePath (Join-Path $root 'agent\api-media-run.ps1') -ArgumentList $data,$script:sharedMedia
                $script:imageBusy=$true;$send.Enabled=$true;$send.Text='Stop';Relayout-Chat
                $this.Tag.dialog.Close()
            }catch{$this.Tag.status.Text=$_.Exception.Message}
        });$dlg.Controls.Add($btn)
    }
    [void]$dlg.ShowDialog($form);$dlg.Dispose()
}
