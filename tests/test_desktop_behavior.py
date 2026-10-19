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
$desktop = Get-Ast (Join-Path $repo 'mamidlabs-desktop.ps1')
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
$script:selectedAudio = [Collections.Generic.List[string]]::new()
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
Restart-LocalMamidLabs 'flash'
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
Restart-LocalMamidLabs 'flash'
$first=$script:switchProcess
$script:apiSettings.selectedLocalModel='local-b'; $script:apiSettings.selectedModel='local-b'
Restart-LocalMamidLabs 'flash'
Stop-LocalMamidLabs
$first.HasExited=$true; Update-ModelSwitch
Assert ($calls.Count -eq 2 -and $calls[1] -eq 'stop' -and !$script:pendingModelChange) 'Stop did not replace queued switch'
""")


def test_lite_max_flash_and_api_provider_switches_reload_or_stop_correctly(tmp_path):
    """Switching between LITE/FLASH/MAX (local) must call Restart-LocalMamidLabs with the
    right profile (Invoke-Manage 'switch'); switching to an API provider (custom
    included) must trigger Stop-LocalMamidLabs (Invoke-Manage 'stop', to free VRAM);
    switching back from an API to local must call 'switch' again. See
    model-registry.ps1 Select-RegisteredModel, mamidlabs-desktop.ps1 Update-ModelSwitch."""
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
Assert (!$script:modelReady -and !$script:activeModel) 'Stop-LocalMamidLabs did not run (modelReady/activeModel were not cleared)'
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
. ([scriptblock]::Create((Get-Functions (Get-Ast (Join-Path $repo 'agent/mamidlabs-inference.ps1')))))
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


def test_microphone_menu_cancel_and_red_mic_send_wiring(tmp_path):
    """While recording, clicking the RED microphone CANCELS the recording (stop + delete file).
    "Finish and send" and "Cancel recording" in the menu also work. When idle the menu opens and
    "Use microphone" starts recording. Right click always opens the menu."""
    run_ps(tmp_path, r"""
Add-Type -AssemblyName System.Windows.Forms
$script:micRecording = $false
$micBtn = [pscustomobject]@{Height=30; BackColor=$null}
$micUse = [pscustomobject]@{Text=''}
$micCancel = [pscustomobject]@{Text=''}
$script:menuShown=$false; $script:menuClosed=$false
$micMenu = [pscustomobject]@{}
$micMenu | Add-Member ScriptMethod Show { param($c,$x,$y) $script:menuShown=$true }
$micMenu | Add-Member ScriptMethod Close { $script:menuClosed=$true }
$script:cancelled=$false; $script:completed=$false; $script:started=$false
function Cancel-MicrophoneCapture { $script:cancelled=$true }
function Complete-MicrophoneCapture { $script:completed=$true }
function Start-MicrophoneCapture { $script:started=$true }

# --- Idle ---
& (Get-Handler '$micBtn' 'Add_Click')
Assert ($script:menuShown) 'Idle mic click did not open the menu'
& (Get-Handler '$micUse' 'Add_Click')
Assert ($script:started -and !$script:completed) 'Idle use item did not start recording'
& (Get-Handler '$micCancel' 'Add_Click')
Assert (!$script:cancelled -and $script:menuClosed) 'Idle cancel cancelled a recording'

# --- Recording: red mic click CANCELS ---
$script:menuShown=$false; $script:menuClosed=$false
$script:micRecording = $true
Show-MicMenu
Assert ($micUse.Text -eq 'Finish and send' -and $micCancel.Text -eq 'Cancel recording') 'Recording menu labels wrong'
$script:menuShown=$false
& (Get-Handler '$micBtn' 'Add_Click')
Assert ($script:cancelled -and !$script:menuShown) 'Red mic click did not cancel the recording'
$script:cancelled=$false
& (Get-Handler '$micUse' 'Add_Click')
Assert ($script:completed) 'Recording use item did not finish+send'
& (Get-Handler '$micCancel' 'Add_Click')
Assert ($script:cancelled) 'Recording cancel item did not cancel'

# Right click always opens the menu.
$script:menuShown=$false
$right = [pscustomobject]@{Button=[System.Windows.Forms.MouseButtons]::Right}
& (Get-Handler '$micBtn' 'Add_MouseUp') $micBtn $right
Assert ($script:menuShown) 'Right click did not open the recording menu'
""")


def test_complete_and_cancel_microphone_capture_attach_and_send(tmp_path):
    """Complete-MicrophoneCapture attaches the recording and sends it; Cancel-MicrophoneCapture
    discards it WITHOUT saving (discard) and deletes the file, sending nothing."""
    wav = tmp_path / "microphone-test.wav"
    wav.write_bytes(b"RIFF....WAVE")
    run_ps(tmp_path, r"""
$path = """ + ps_quote(wav) + r"""
$script:selectedAudio = [Collections.Generic.List[string]]::new()
$script:added=$null; $script:discard=$null
function Add-SelectedAudio([string]$p) { $script:added=$p }
$script:clicked=$false
$send = [pscustomobject]@{Text='Send'}
$send | Add-Member ScriptMethod PerformClick { $script:clicked=$true }
$script:busy=$false; $script:pendingSend=$false; $script:imageBusy=$false
function Stop-MicrophoneCapture([switch]$discard) { $script:discard=$discard; return $path }

Complete-MicrophoneCapture
Assert ($script:added -eq $path) 'Complete did not attach the recording'
Assert ($script:clicked) 'Complete did not auto-send'
Assert (!$script:discard) 'Complete discarded instead of saving'

$script:clicked=$false
Cancel-MicrophoneCapture
Assert ($script:discard) 'Cancel did not discard the recording'
Assert (!$script:clicked) 'Cancel sent something'
Assert (!(Test-Path -LiteralPath $path)) 'Cancel did not delete the recording file'
""")


def test_audio_turn_preempts_model_load_and_gate_holds_without_audio(tmp_path):
    """An audio turn starts Begin-Generation IMMEDIATELY even if the language model is not ready
    and abandons the in-flight model load (Stop-ModelSwitchForAudio). Without audio the old gate
    is kept."""
    run_ps(tmp_path, r"""
$script:killed=$false
$proc = [pscustomobject]@{HasExited=$false}
$proc | Add-Member ScriptMethod Kill { param($tree) $script:killed=$true }
$proc | Add-Member ScriptMethod WaitForExit { param($ms) return $true }
$proc | Add-Member ScriptMethod Dispose {}
$script:switchProcess=$proc
$script:pendingModelChange=@{action='switch'}
$script:modelReady=$true; $script:activeModel='qwen3.5-9b-lite'
$script:modelSwitchError='old error'
Stop-ModelSwitchForAudio
Assert ($script:killed) 'In-flight model load was not killed'
Assert (!$script:switchProcess -and !$script:pendingModelChange) 'Switch state was not cleared'
Assert (!$script:modelReady -and !$script:activeModel) 'Model readiness was not reset for ASR'
Assert (!$script:modelSwitchError) 'Preempt kept a stale switch error'

# --- No audio: the gate is kept (wait until the model is ready) ---
$script:proceeded=$false; $script:preempted=$false
function Stop-ModelSwitchForAudio { $script:preempted=$true }
function Stop-BackgroundCompaction { $script:proceeded=$true }
function Build-JobMessages { throw 'STOP' }
$script:providerId='local'; $script:modelReady=$false; $script:busy=$false; $script:imageBusy=$false
$script:pendingSend=$true; $script:currentTurnAudioPaths=@()
Begin-Generation
Assert (!$script:proceeded -and !$script:preempted) 'Audio-less turn bypassed the ready-model gate'
Assert ($script:pendingSend) 'Gate dropped the queued message'

# --- With audio: it starts immediately and the load is preempted ---
function Get-ProfileFromAlias([string]$a) { return 'lite' }
function Get-SelectedLocalAlias { return 'qwen3.5-9b-lite' }
$script:proceeded=$false; $script:preempted=$false
$script:pendingSend=$true; $script:currentTurnAudioPaths=@('audio.wav')
# The rest of Begin-Generation (job setup) is deliberately stopped early.
try { Begin-Generation } catch { }
Assert ($script:preempted) 'Audio turn did not preempt the model load'
Assert ($script:proceeded) 'Audio turn did not start immediately'
Assert (!$script:pendingSend) 'Audio turn stayed queued'
""")


def test_model_switch_error_surfaces_the_specific_reason(tmp_path):
    """The manager process's stderr is carried to the UI: a specific cause such as
    insufficient VRAM is shown instead of the generic "Model operation failed"; an empty
    stderr falls back to the generic message (see Format-ModelSwitchError)."""
    run_ps(tmp_path, r"""
$msg = Format-ModelSwitchError "info: something`nNot enough free VRAM: 14763 MiB free, the `"flash`" profile needs 15200 MiB"
Assert ($msg -match 'Not enough free VRAM') 'Specific VRAM reason not surfaced'
Assert ($msg -notmatch 'Model operation failed') 'Generic message shown despite a specific reason'
$empty = Format-ModelSwitchError ''
Assert ($empty -match 'Model operation failed') 'Empty stderr must fall back to the generic message'
""")


def test_desktop_captures_manager_stderr_for_specific_errors():
    """The UI captures the manager process's stderr and runs it through
    Format-ModelSwitchError, so the real cause (e.g. VRAM) is shown instead of generic."""
    desktop = (ROOT / "mamidlabs-desktop.ps1").read_text(encoding="utf-8-sig")
    assert "RedirectStandardError = $true" in desktop
    assert "function Format-ModelSwitchError" in desktop
    assert "Format-ModelSwitchError $detail" in desktop


def test_desktop_reads_the_real_context_back_from_the_server():
    """MAX/FLASH may load with a SMALLER context than the UI default: manage-mamidlabs.ps1 tries
    32K and falls back to 16K when 32K does not fully fit VRAM. The compaction threshold must
    follow the server's REAL context, otherwise a long turn would exceed --ctx-size and fail. So
    when a model becomes ready the UI reads /props' n_ctx and updates ctxLimit/compactAt."""
    desktop = (ROOT / "mamidlabs-desktop.ps1").read_text(encoding="utf-8-sig")
    assert "$script:effectiveCtxAlias" in desktop
    assert "if ($script:activeModel -ne $script:effectiveCtxAlias)" in desktop
    assert "default_generation_settings.n_ctx" in desktop
    # The read-back must be wired to the model-ready transition, right where modelReady is set.
    idx_ready = desktop.index("$script:modelReady = $true")
    idx_props = desktop.index("default_generation_settings.n_ctx")
    assert idx_ready < idx_props
    # compactAt is recomputed from the REAL context, not left at the 32K default.
    assert "$script:compactAt = [int]($realCtx * 0.67)" in desktop


def test_child_process_stderr_is_read_as_utf8_and_boxes_are_stripped(tmp_path):
    """Regression (the status text beside "Full access"): the model-manager process's stderr was
    written with the OEM code page; the UI assumed UTF-8 and decoded it, turning non-ASCII
    characters into "boxes" (U+FFFD). Now both sides are explicitly UTF-8: the child process
    WRITES with `[Console]::OutputEncoding = UTF8`, the UI READS with
    `StandardErrorEncoding = UTF8`. Also, U+FFFD never reaches the UI (`Repair-UiText`)."""
    desktop = (ROOT / "mamidlabs-desktop.ps1").read_text(encoding="utf-8-sig")
    assert "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)" in desktop
    assert "$OutputEncoding = [Text.UTF8Encoding]::new($false)" in desktop
    assert "$psi.StandardErrorEncoding = [Text.Encoding]::UTF8" in desktop
    # NOTE: StandardOutputEncoding can only be set when RedirectStandardOutput is enabled;
    # otherwise .NET throws and the model can NEVER be started.
    invoke_manage = desktop.split("function Invoke-Manage")[1].split("function ")[0]
    if "$psi.StandardOutputEncoding =" in invoke_manage:
        assert "RedirectStandardOutput = $true" in invoke_manage, (
            "Invoke-Manage uses StandardOutputEncoding without redirecting stdout "
            "(.NET exception -> the model cannot load)"
        )
    # The child process (the manager) must also write UTF-8; otherwise the parent decoding UTF-8
    # corrupts the text.
    manager = (ROOT / "scripts" / "manage-mamidlabs.ps1").read_text(encoding="utf-8-sig")
    assert "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)" in manager
    # Python tools must write UTF-8 to the pipe (otherwise non-ASCII in tool results is corrupt).
    worker = (ROOT / "agent" / "mamidlabs-inference.ps1").read_text(encoding="utf-8-sig")
    assert "$psi.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'" in worker
    assert "$psi.StandardErrorEncoding = [Text.Encoding]::UTF8" in worker
    # Real pwsh: U+FFFD is stripped; correct non-ASCII text is UNCHANGED.
    body = r"""
$broken = 'Not enough free VRAM: ' + [string][char]0xFFFD + 'sample ' + [string][char]0xFFFD + 'text'
$clean = Repair-UiText $broken
Assert ($clean -notlike "*$([string][char]0xFFFD)*") 'U+FFFD was not stripped'
Assert ($clean -like 'Not enough free VRAM:*') 'text was not preserved'
$good = 'Résumé café — agent turn 3/8 ✓'
Assert ((Repair-UiText $good) -eq $good) 'correct text was changed'
Assert ((Format-ModelSwitchError $broken) -notlike "*$([string][char]0xFFFD)*") 'the formatter did not clean it'
'OK'
"""
    out = run_ps(tmp_path, body)
    assert "OK" in out


def test_audio_attach_stops_model_load_and_warms_asr(tmp_path):
    """The first audio turn must not be heavy: AS SOON AS an audio attachment is added
    (a) an in-flight language-model load is stopped (local provider + model not ready) and
    (b) the warm ASR worker is started in the background. Also, when the load is stopped,
    `manage -Action stop` kills llama-server too (so a detached load does not hold the GPU)."""
    desktop = (ROOT / "mamidlabs-desktop.ps1").read_text(encoding="utf-8-sig")
    assert "function Prepare-AudioTurn" in desktop
    assert "function Start-AsrWarmForAudio" in desktop
    add = desktop.split("function Add-SelectedAudio")[1].split("function ")[0]
    assert "Prepare-AudioTurn" in add
    prep = desktop.split("function Prepare-AudioTurn")[1].split("function ")[0]
    assert "$script:providerId -eq 'local' -and !$script:modelReady" in prep
    assert "Stop-ModelSwitchForAudio" in prep
    assert "Start-AsrWarmForAudio" in prep
    warm = desktop.split("function Start-AsrWarmForAudio")[1].split("function ")[0]
    assert "'--serve'" in warm and "'--parent-pid'" in warm
    assert "logs\\asr-server.json" in warm
    stop = desktop.split("function Stop-ModelSwitchForAudio")[1].split("function ")[0]
    assert "'-Action', 'stop'" in stop
    # Behavior: local + model not ready -> the load stops + ASR starts; if the model is ready the
    # load does NOT stop.
    body = r"""
$script:providerId = 'local'; $script:modelReady = $false
$script:calls = @()
function Stop-ModelSwitchForAudio { $script:calls += 'stop-switch' }
function Start-AsrWarmForAudio { $script:calls += 'warm-asr' }
Prepare-AudioTurn
$script:modelReady = $true
Prepare-AudioTurn
Assert (($script:calls -join ',') -eq 'stop-switch,warm-asr,warm-asr') ('unexpected: ' + ($script:calls -join ','))
'OK'
"""
    out = run_ps(tmp_path, body)
    assert "OK" in out

