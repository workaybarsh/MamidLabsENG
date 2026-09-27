"""Execute production PowerShell functions/events with UI and model I/O doubles.

These are behavior tests, not a replacement for Windows/WinForms rendering tests.
"""
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def run_ps(tmp_path, body):
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")
    prelude = r"""
$ErrorActionPreference = 'Stop'
function Get-Ast([string]$path) {
    $tokens = $null; $errors = $null
    $ast = [Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors)
    if ($errors.Count) { throw ($errors | Out-String) }
    return $ast
}
function Get-Functions($ast) {
    return (($ast.EndBlock.Statements | Where-Object {
        $_ -is [Management.Automation.Language.FunctionDefinitionAst]
    } | ForEach-Object { $_.Extent.Text }) -join "`n")
}
function Get-Handler([string]$control, [string]$event) {
    $node = $desktop.FindAll({ param($n)
        $n -is [Management.Automation.Language.InvokeMemberExpressionAst] -and
        $n.Expression.Extent.Text -eq $control -and $n.Member.Value -eq $event
    }, $true) | Select-Object -First 1
    if (!$node) { throw "Missing handler: $control.$event" }
    return $node.Arguments[0].ScriptBlock.GetScriptBlock()
}
function Assert($condition, [string]$message) { if (!$condition) { throw $message } }
$desktop = Get-Ast (Join-Path $repo 'qwen-desktop.ps1')
. ([scriptblock]::Create((Get-Functions $desktop)))
. ([scriptblock]::Create((Get-Functions (Get-Ast (Join-Path $repo 'agent/api-providers.ps1')))))
. ([scriptblock]::Create((Get-Functions (Get-Ast (Join-Path $repo 'agent/model-registry.ps1')))))
. ([scriptblock]::Create((Get-Functions (Get-Ast (Join-Path $repo 'agent/api-settings.ps1')))))
$metric = @{Text=''}; $statusCtrl = @{Text=''}; $send = @{Text='Send'}
$netChip = @{Checked=$true}; $agentChip = @{Checked=$false}; $fullAccessChip = @{Checked=$false}
function Update-LiveMetrics {}
"""
    script = tmp_path / "test.ps1"
    script.write_text("$repo = " + ps_quote(ROOT) + "\n" + prelude + body, encoding="utf-8-sig")
    result = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-File", str(script)],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
        # pwsh writes UTF-8; without an explicit encoding the Windows system code page is used
        # and on a non-UTF-8 locale (e.g. cp1254) a decoding error leaves stdout as None.
        encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_full_access_round_trips_with_preferences_and_defaults(tmp_path):
    run_ps(tmp_path, r"""
$root = (Get-Location).Path
$script:apiSettings = @{ providers=@{}; uiPreferences=@{internet=$false;agent=$true;accessMode='full'} }
Save-ApiSettings
$script:apiSettings = Read-ApiSettings
Restore-UiPreferences
Assert ($fullAccessChip.Checked -and $script:accessMode -eq 'full') 'Full access was not restored'
Assert (!$netChip.Checked -and $agentChip.Checked) 'Other preferences changed'
$fullAccessChip.Checked = $false; $script:accessMode = 'ask'
Save-UiPreferences
$script:apiSettings = Read-ApiSettings
Restore-UiPreferences
Assert (!$fullAccessChip.Checked -and $script:accessMode -eq 'ask') 'Disabled access was not persisted'
foreach ($invalid in @('unexpected', '', $null)) {
    $script:apiSettings.uiPreferences = @{accessMode=$invalid}
    Restore-UiPreferences
    Assert (!$fullAccessChip.Checked -and $script:accessMode -eq 'ask') 'Invalid preference grants access'
}
$script:apiSettings = @{providers=@{}}
Restore-UiPreferences
Assert (!$fullAccessChip.Checked -and !$script:restoringUiPreferences) 'Missing preference grants access'
Assert (!(Test-Path 'settings/api.json.tmp')) 'Temporary settings file leaked'
""")


CHAT_DOUBLES = r"""
$bubbles = [Collections.ArrayList]::new(); $chatMessages = [Collections.ArrayList]::new()
$script:selectedImages = [Collections.Generic.List[string]]::new()
$script:selectedVideos = [Collections.Generic.List[string]]::new()
$script:selectedDocuments = [Collections.Generic.List[string]]::new()
$script:attachedDocuments = [Collections.Generic.List[string]]::new()
$promptBox = @{Text='turn on multi-image reading mode'}; $clearImage = @{Visible=$false}
$chatInner = [pscustomobject]@{Height=0; Width=300}
$chatInner | Add-Member ScriptMethod SuspendLayout {}
$chatInner | Add-Member ScriptMethod ResumeLayout {}
$chatScroll = @{ClientSize=@{Width=600;Height=100}; AutoScrollPosition=@{Y=0}}
$script:saves = 0
function Save-Chat { $script:saves++ }
function Scroll-ChatToEnd {}
function Update-Bubble($b) { $b.panel.Height = if ($b.role -eq 'notice') { 90 } else { 60 } }
function New-Bubble($role) {
    $b = @{role=$role; body=@{Text=''}; panel=@{Top=0;Height=60}; artifacts=[Collections.ArrayList]::new();rawText=''}
    [void]$bubbles.Add($b)
    return $b
}
function Set-BubbleAttachmentList($b, $paths) { foreach($path in $paths) { Add-BubbleArtifact $b $path } }
function Convert-ImageToDataUri($path) { return 'data:image/png;base64,dGVzdA==' }
function Begin-Generation { $script:began = $true }
"""


@pytest.mark.parametrize("with_images", [False, True])
def test_multi_image_notice_survives_tick_and_is_not_model_history(tmp_path, with_images):
    run_ps(tmp_path, CHAT_DOUBLES + (
        "$script:selectedImages.Add('first.png'); $script:selectedImages.Add('second.png')\n"
        if with_images else ""
    ) + r"""
$script:activeBubble = New-Bubble 'assistant'
$script:activeBubble.body.Text = 'Earlier answer'
$script:providerId = 'local'; $script:modelReady = $false
& (Get-Handler '$send' 'Add_Click')
Assert $script:multiImageOcrMode 'Mode did not activate'
$notices = @($bubbles | Where-Object {$_.role -eq 'notice'})
Assert ($notices.Count -eq 1) 'Expected one persistent notification'
Assert ($notices[0].body.Text -like 'Multi-image reading on*') 'Notification text missing'
Assert (@($chatMessages | Where-Object {$_.role -ne 'user'}).Count -eq 0) 'UI event leaked into model history'
& (Get-Handler '$chatPoll' 'Add_Tick')
$last = $bubbles[$bubbles.Count-1]
Assert ($chatInner.Height -ge ($last.panel.Top + $last.panel.Height)) 'Old active bubble clipped the notice'
Assert ($script:saves -gt 0) 'Notice was not saved'
""" + (
        "Assert $script:currentTurnMultiImageOcr 'Images did not activate OCR for this turn'\n"
        if with_images else r"""
$promptBox.Text = 'turn on multi-image reading mode'
& (Get-Handler '$send' 'Add_Click')
Assert (@($bubbles | Where-Object {$_.role -eq 'notice'}).Count -eq 1) 'Repeated activation duplicated notice'
"""
    ))


def test_failed_image_conversion_does_not_activate_mode_or_add_notice(tmp_path):
    run_ps(tmp_path, CHAT_DOUBLES + r"""
$script:selectedImages.Add('bad.png'); $script:selectedImages.Add('second.png')
function Convert-ImageToDataUri($path) { throw 'broken image' }
& (Get-Handler '$send' 'Add_Click')
Assert (!$script:multiImageOcrMode -and $bubbles.Count -eq 0) 'Failed send activated or displayed mode'
Assert ($script:selectedImages.Count -eq 2) 'Failed send discarded attachments'
""")


def test_model_choice_is_immediate_serialized_and_health_matches_target(tmp_path):
    run_ps(tmp_path, r"""
$script:apiSettings = @{providers=@{}; selectedModel='builtin-max'; localProfile='max'}
$script:providerId = 'local'; $script:modelReady = $true; $script:activeModel = 'qwen3.8-27b-max'
$script:modelEntries = @(
    @{id='builtin-flash';provider='local';profile='flash';label='FLASH';contextLimit=32768},
    @{id='local-a';provider='local';profile='flash';path='a.gguf';label='A';contextLimit=8192},
    @{id='local-b';provider='local';profile='flash';path='b.gguf';label='B';contextLimit=8192}
)
$modelChoice = @{SelectedIndex=0;SelectedItem='FLASH'}
$calls = [Collections.ArrayList]::new()
function Save-ApiSettings {}
function Cancel-Generation {}
function Invoke-Manage($action, $profile, $modelId) {
    [void]$calls.Add(@{action=$action;profile=$profile;modelId=$modelId})
    $p = [pscustomobject]@{HasExited=$false;ExitCode=0;disposed=$false}
    $p | Add-Member ScriptMethod Dispose {$this.disposed=$true}
    return $p
}
Select-RegisteredModel
Assert ($script:apiSettings.selectedModel -eq 'builtin-flash' -and !$script:modelReady) 'Choice must precede loading'
Assert ($metric.Text -like '*FLASH*') 'Loading status lacks selected model'
$first = $script:switchProcess
$modelChoice.SelectedIndex=1; $modelChoice.SelectedItem='A'; Select-RegisteredModel
$modelChoice.SelectedIndex=2; $modelChoice.SelectedItem='B'; Select-RegisteredModel
Assert ($calls.Count -eq 1) 'Switches ran concurrently'
Assert ($script:apiSettings.selectedModel -eq 'local-b') 'Latest custom model not selected'
$first.HasExited=$true
Update-ModelSwitch
Assert ($first.disposed -and $calls.Count -eq 2 -and $calls[1].modelId -eq 'local-b') 'Latest choice was not launched'
$script:switchProcess.HasExited=$true
$script:reportedAlias='qwen3.8-27b-max'; $script:pendingSend=$true
function Invoke-RestMethod { return @{status='ok'} }
function Get-ActiveModel { return $script:reportedAlias }
function Get-Process { return @() }
function Begin-Generation { $script:began=$true; $script:pendingSend=$false }
& (Get-Handler '$timer' 'Add_Tick')
Assert (!$script:modelReady -and !$script:began) 'Message sent to old model'
Assert ($modelChoice.SelectedIndex -eq 2) 'Health poll reverted the selection'
$script:reportedAlias='local-b'
& (Get-Handler '$timer' 'Add_Tick')
Assert ($script:modelReady -and $script:began -and $script:activeModel -eq 'local-b') 'Target model not accepted'
$script:pendingSend=$false; $script:modelReady=$false
Restart-LocalQwen 'flash'
$script:switchProcess.HasExited=$true; $script:switchProcess.ExitCode=1
& (Get-Handler '$timer' 'Add_Tick')
Assert (!$script:modelReady -and $script:modelSwitchError -and $modelChoice.SelectedIndex -eq 2) 'Failed switch hid error or changed choice'
""")


def test_stop_supersedes_queued_model_load(tmp_path):
    run_ps(tmp_path, r"""
$script:apiSettings = @{selectedModel='local-a';selectedLocalModel='local-a'}; $script:providerId='local'
$modelChoice=@{SelectedItem='A'}
$calls=[Collections.ArrayList]::new()
function Cancel-Generation {}
function Invoke-Manage($action,$profile,$modelId) {
    [void]$calls.Add($action)
    $p=[pscustomobject]@{HasExited=$false;ExitCode=0}
    $p | Add-Member ScriptMethod Dispose {}
    return $p
}
Restart-LocalQwen 'flash'
$first=$script:switchProcess
$script:apiSettings.selectedLocalModel='local-b'; $script:apiSettings.selectedModel='local-b'
Restart-LocalQwen 'flash'
Stop-LocalQwen
$first.HasExited=$true; Update-ModelSwitch
Assert ($calls.Count -eq 2 -and $calls[1] -eq 'stop' -and !$script:pendingModelChange) 'Stop did not replace queued switch'
""")


def test_lite_max_flash_and_api_provider_switches_reload_or_stop_correctly(tmp_path):
    """Switching between LITE/FLASH/MAX (local) must call Restart-LocalQwen with the
    right profile (Invoke-Manage 'switch'); switching to an API provider (custom
    included) must trigger Stop-LocalQwen (Invoke-Manage 'stop', to free VRAM);
    switching back from an API to local must call 'switch' again. See
    model-registry.ps1 Select-RegisteredModel, qwen-desktop.ps1 Update-ModelSwitch."""
    run_ps(tmp_path, r"""
$script:apiSettings = @{providers=@{custom=@{model='llama-3-local'}}; selectedModel='builtin-lite'; localProfile='lite'}
$script:providerId = 'local'; $script:modelReady = $false; $script:activeModel = $null
$script:modelEntries = @(
    @{id='builtin-max';provider='local';profile='max';label='Qwen3.8 MAX';contextLimit=32768},
    @{id='builtin-flash';provider='local';profile='flash';label='Qwen3.8 FLASH';contextLimit=32768},
    @{id='builtin-lite';provider='local';profile='lite';label='Qwen3.5 9B LITE';contextLimit=32768},
    @{id='custom|llama-3-local';provider='custom';profile='';label='custom · llama-3-local';model='llama-3-local';contextLimit=32768}
)
$modelChoice = @{SelectedIndex=2;SelectedItem='Qwen3.5 9B LITE'}
$manageCalls = [Collections.ArrayList]::new()
function Save-ApiSettings {}
function Cancel-Generation {}
function Invoke-Manage($action, $profile, $modelId) {
    [void]$manageCalls.Add(@{action=$action;profile=$profile;modelId=$modelId})
    $p = [pscustomobject]@{HasExited=$false;ExitCode=0;disposed=$false}
    $p | Add-Member ScriptMethod Dispose {$this.disposed=$true}
    return $p
}
# Update-ModelSwitch does not apply the next pending change until the previous manage
# process is considered "finished" (real behavior: one process at a time).
function Finish-Switch { $script:switchProcess.HasExited = $true; Update-ModelSwitch }

# LITE -> MAX: local to local.
$modelChoice.SelectedIndex=0; $modelChoice.SelectedItem='Qwen3.8 MAX'; Select-RegisteredModel
Assert ($script:apiSettings.selectedModel -eq 'builtin-max' -and $script:apiSettings.localProfile -eq 'max') 'MAX was not selected'
Assert ($manageCalls.Count -eq 1 -and $manageCalls[0].action -eq 'switch' -and $manageCalls[0].profile -eq 'max') 'Invoke-Manage was not called for MAX'
Finish-Switch

# MAX -> FLASH: local to local again.
$modelChoice.SelectedIndex=1; $modelChoice.SelectedItem='Qwen3.8 FLASH'; Select-RegisteredModel
Assert ($manageCalls.Count -eq 2 -and $manageCalls[1].action -eq 'switch' -and $manageCalls[1].profile -eq 'flash') 'Invoke-Manage was not called for FLASH'
Assert ($script:apiSettings.localProfile -eq 'flash') 'localProfile did not become flash'
Finish-Switch

# FLASH -> custom (API): the local model must be stopped (to free VRAM, Invoke-Manage 'stop').
$modelChoice.SelectedIndex=3; $modelChoice.SelectedItem='custom · llama-3-local'; Select-RegisteredModel
Assert ($script:providerId -eq 'custom' -and $script:apiSettings.selected -eq 'custom') 'did not switch to the custom provider'
Assert ($script:apiSettings.selectedModel -eq 'custom|llama-3-local') 'the custom model id was not saved'
Assert (!$script:modelReady -and !$script:activeModel) 'Stop-LocalQwen did not run (modelReady/activeModel were not cleared)'
Assert ($manageCalls.Count -eq 3 -and $manageCalls[2].action -eq 'stop') 'the local model was not stopped on the API switch'
Finish-Switch

# custom (API) -> LITE: switching back to local must trigger 'switch' again.
$modelChoice.SelectedIndex=2; $modelChoice.SelectedItem='Qwen3.5 9B LITE'; Select-RegisteredModel
Assert ($script:providerId -eq 'local' -and $script:apiSettings.localProfile -eq 'lite') 'did not switch back to the LITE profile'
Assert ($manageCalls.Count -eq 4 -and $manageCalls[3].action -eq 'switch' -and $manageCalls[3].profile -eq 'lite') 'Invoke-Manage was not called for LITE'
""")


def test_all_attached_documents_are_authorized_but_not_their_siblings(tmp_path):
    first = tmp_path / "one.docx"
    second = tmp_path / "two.docx"
    sibling = tmp_path / "private.docx"
    for file in (first, second, sibling):
        file.write_bytes(b"fixture")
    run_ps(tmp_path, r"""
. ([scriptblock]::Create((Get-Functions (Get-Ast (Join-Path $repo 'agent/qwen-inference.ps1')))))
function Invoke-PythonTool($payload) { return $payload.path }
$script:attachedDocuments = @(
""" + ps_quote(first) + "," + ps_quote(second) + r"""
)
foreach ($path in $script:attachedDocuments) {
    Assert ((Invoke-Tool 'read_document' @{path=$path} '') -eq $path) "Attached document denied: $path"
}
$denied = Invoke-Tool 'read_document' @{path=
""" + ps_quote(sibling) + r"""
} ''
Assert ($denied -like 'This folder is not open for reading*') 'Sibling file gained implicit permission'
""")


def test_chat_round_trip_restores_attachment_lists_documents_and_notices(tmp_path):
    run_ps(tmp_path, CHAT_DOUBLES + r"""
# Restore production saving; replace only Windows DPAPI and visual controls.
. ([scriptblock]::Create(($desktop.EndBlock.Statements | Where-Object {$_.Name -eq 'Save-Chat'}).Extent.Text))
$chatDir=(Get-Location).Path; $script:chatId='0123456789abcdef0123456789abcdef'
function Protect-ChatText($text) { return $text }
$user=New-Bubble 'user'; $user.body.Text='Compare these files'
foreach($path in @('first.docx','second.xlsx','missing.png')) { Add-BubbleArtifact $user $path }
$notice=New-Bubble 'notice'; $notice.body.Text='Multi-image reading on'
[void]$chatMessages.Add(@{role='user';content='Compare these files'})
Save-Chat
$saved=Get-Content "$chatDir/$($script:chatId).json" -Raw | ConvertFrom-Json
Assert ($saved.transcript.Count -eq 2 -and $saved.transcript[1].role -eq 'notice') 'Notice not persisted'
function Clear-ChatView { $bubbles.Clear(); $chatMessages.Clear(); $script:attachedDocuments.Clear(); $script:multiImageOcrMode=$false }
function Cancel-Generation {}
$script:rendered=[Collections.ArrayList]::new()
function Set-BubbleAttachmentList($b,$paths) { [void]$script:rendered.Add(@($paths)) }
Load-Chat "$chatDir/$($script:chatId).json"
Assert ($script:rendered.Count -eq 1 -and $script:rendered[0].Count -eq 3) 'Mixed/missing attachment list lost'
Assert ($script:attachedDocuments.Count -eq 2 -and $script:lastUserImage -eq 'missing.png') 'Source attachments not restored'
Assert ($bubbles[1].role -eq 'notice' -and !$script:multiImageOcrMode) 'History wrongly re-enabled OCR mode'
Assert ($chatMessages.Count -eq 1) 'UI notices were sent as model history'
# Verify replacement, not just first creation.
$bubbles[0].body.Text='Updated'
Save-Chat
$saved=Get-Content "$chatDir/$($script:chatId).json" -Raw | ConvertFrom-Json
Assert ($saved.transcript[0].text -eq 'Updated') 'Existing chat not replaced'
Assert (!(Test-Path "$chatDir/$($script:chatId).json.tmp")) 'Temporary chat leaked'
""")


def test_failed_chat_save_keeps_old_file_and_reports_error(tmp_path):
    run_ps(tmp_path, CHAT_DOUBLES + r"""
# The MessageBox call is redirected to a fake function. Defining a fake type with the same
# name via Add-Type does not work when pwsh loads the real WinForms at startup; a real window
# opened and the test timed out after 30 s.
$saveChat = ($desktop.EndBlock.Statements | Where-Object {$_.Name -eq 'Save-Chat'}).Extent.Text
if (!$saveChat.Contains('[System.Windows.Forms.MessageBox]::Show(')) { throw 'Save-Chat no longer reports errors via MessageBox' }
class TestMessageBox { static [string]$Last; static [object] Show([string]$text, [string]$title) { [TestMessageBox]::Last = $text; return $null } }
. ([scriptblock]::Create($saveChat.Replace('[System.Windows.Forms.MessageBox]::Show(', '[TestMessageBox]::Show(')))
$chatDir=(Get-Location).Path; $script:chatId='0123456789abcdef0123456789abcdef'
$path=Join-Path $chatDir "$($script:chatId).json"
[IO.File]::WriteAllText($path,'previous encrypted chat')
function Protect-ChatText($text) { return 'replacement' }
# A directory in place of the temporary file makes the actual write fail.
[void][IO.Directory]::CreateDirectory($path+'.tmp')
$b=New-Bubble 'user'; $b.body.Text='New text'
Save-Chat
Assert ([IO.File]::ReadAllText($path) -eq 'previous encrypted chat') 'Previous chat was overwritten on failed write'
Assert ([TestMessageBox]::Last -like 'The chat could not be saved*') 'Save failure was silent'
""")
