"""Agent permission flow (two bugs found in a real UI test).

1) When the approval was not given within 10 min the tool side gave up with "timeout" but
   the MessageBox in the UI stayed open, and a later "Yes" was silently
   ignored. Now every question carries an AskId; the window closes when the id changes.
2) After a denied create_document the model said "The document is ready", there was no file.
   The tool result now carries an explicit order and a note independent of the model goes to the UI.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8-sig")


def run_pwsh(tmp_path, body: str) -> dict:
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh not found")
    script = tmp_path / "approval.ps1"
    script.write_text(
        f"""
# Output is read as UTF-8; the console code page depends on the calling shell (it got garbled in Git Bash).
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$job = @{{ messages = @(@{{ role = 'user'; content = 'hello' }}); compactOnly = $true; provider = 'local'; model = 'test'; contextLimit = 32768; maxTokens = 100; agent = $true; internet = $false; office = $false; documentCreate = $true }}
$shared = [hashtable]::Synchronized(@{{ Cancel = $false; AccessMode = 'ask'; AskTool = $null; AskDetail = $null; AskAnswer = $null; AskId = $null; AskDeadline = $null }})
. '{ROOT}\\agent\\qwen-inference.ps1' $job $shared | Out-Null
{body}
""",
        encoding="utf-8-sig",
    )
    result = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-File", str(script)],
        capture_output=True, encoding="utf-8", errors="replace", check=False, timeout=90,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_timed_out_document_request_is_reported_as_not_done(tmp_path):
    got = run_pwsh(tmp_path, f"""
$script:ApprovalTimeout = [TimeSpan]::FromSeconds(1)
$arguments = [pscustomobject]@{{ filename = 'summary.docx'; format = 'docx'; content = '# Test' }}
$result = Invoke-Tool 'create_document' $arguments '{tmp_path}'
@{{ result = $result; note = $script:deniedNote; outcome = $script:lastApprovalOutcome
   askId = $shared.AskId; askTool = $shared.AskTool; exists = (Test-Path '{tmp_path}\\summary.docx') }} | ConvertTo-Json -Compress
""")
    assert got["outcome"] == "timeout"
    assert got["result"].startswith("NOTHING WAS DONE: the document was not created")
    assert "do not say it was done, is ready or is waiting for permission" in got["result"]
    assert got["note"].startswith("Nothing was done: document not created")
    assert "permission not given in time" in got["note"]
    # The fields that trigger closing the UI window must have been cleared.
    assert got["askId"] is None and got["askTool"] is None
    assert got["exists"] is False


def test_refused_command_is_reported_as_not_done(tmp_path):
    got = run_pwsh(tmp_path, """
# Instead of the UI: a thread that answers "No" when the question arrives (when AskId is assigned).
$askIds = [Collections.Concurrent.ConcurrentBag[string]]::new()
$responder = Start-ThreadJob -ArgumentList $shared, $askIds -ScriptBlock {
    param($s, $ids)
    $end = [DateTime]::UtcNow.AddSeconds(20)
    while (!$s.AskId -and [DateTime]::UtcNow -lt $end) { Start-Sleep -Milliseconds 50 }
    $ids.Add([string]$s.AskId); $s.AskAnswer = 'no'
}
$arguments = [pscustomobject]@{ command = 'echo hello' }
$result = Invoke-Tool 'run_command' $arguments $env:TEMP
Wait-Job $responder -Timeout 30 | Out-Null
@{ result = $result; note = $script:deniedNote; outcome = $script:lastApprovalOutcome
   askIdSeen = @($askIds)[0]; askId = $shared.AskId } | ConvertTo-Json -Compress
""")
    assert got["outcome"] == "asked_no"
    assert re.fullmatch(r"[0-9a-f]{32}", got["askIdSeen"])
    assert got["askId"] is None
    assert got["result"].startswith("NOTHING WAS DONE: the command was not run")
    assert got["note"] == "Nothing was done: command not run (permission not granted)."


def test_refused_tool_is_not_asked_again_in_the_same_request(tmp_path):
    """Real UI test: after "No" the model tried the same command 4 times, it was asked again
    every time and the model wrongly interpreted the denial as "the output could not be captured"."""
    got = run_pwsh(tmp_path, """
$responder = Start-ThreadJob -ArgumentList $shared -ScriptBlock {
    param($s)
    $end = [DateTime]::UtcNow.AddSeconds(20)
    while (!$s.AskId -and [DateTime]::UtcNow -lt $end) { Start-Sleep -Milliseconds 50 }
    $s.AskAnswer = 'no'
}
$arguments = [pscustomobject]@{ command = 'echo hello' }
$first = Invoke-Tool 'run_command' $arguments $env:TEMP
Wait-Job $responder -Timeout 30 | Out-Null
$firstNote = $script:deniedNote
# Second attempt: nobody answers; if the question were opened it would wait for the timeout (10 min).
$sw = [Diagnostics.Stopwatch]::StartNew()
$second = Invoke-Tool 'run_command' ([pscustomobject]@{ command = 'py hello.py' }) $env:TEMP
@{ first = $first; firstNote = $firstNote; second = $second; secondNote = $script:deniedNote
   outcome = $script:lastApprovalOutcome; askId = $shared.AskId; ms = $sw.ElapsedMilliseconds } | ConvertTo-Json -Compress
""")
    assert "the user's decision" in got["first"] and "Do not retry" in got["first"]
    assert got["firstNote"] == "Nothing was done: command not run (permission not granted)."
    assert got["outcome"] == "repeat_denied"
    assert got["askId"] is None and got["ms"] < 5000
    assert got["second"].startswith("NOTHING WAS DONE") and "it was not asked again" in got["second"]
    assert got["secondNote"] is None


def test_desktop_uses_self_closing_approval_dialog_instead_of_messagebox():
    desktop = read_text("qwen-desktop.ps1")
    assert "The agent wants to make a change on your computer" not in desktop.split("function Show-ApprovalDialog")[0]
    assert "[System.Windows.Forms.MessageBox]::Show(\n                \"The agent" not in desktop
    update = desktop.split("function Update-ApprovalDialog")[1].split("function Show-ApprovalDialog")[0]
    # When the tool side gives up (id changed / cancelled) the window closes itself.
    assert "[string]$ctx.request.AskId -ne $ctx.askId -or $ctx.request.Cancel" in update
    assert "DialogResult]::Abort" in update
    dialog = desktop.split("function Show-ApprovalDialog")[1].split("function Show-ChatHistory")[0]
    assert "$approvalTimer.Add_Tick({ Update-ApprovalDialog $this.Tag })" in dialog
    # Only when the tool side gives up (Abort) is there no answer; X/Esc count as "no" like the
    # old MessageBox, otherwise the UI would reopen the same question right away.
    assert "DialogResult]::Abort) { return $null }\n    return 'no'" in dialog
    # A late answer is not written.
    assert "if ($answer -and [string]$request.AskId -eq $askId)" in desktop


def test_denied_note_is_shown_but_not_sent_back_to_the_model():
    desktop = read_text("qwen-desktop.ps1")
    inference = read_text("agent/qwen-inference.ps1")
    assert "if ($script:deniedNote) { Emit 'denied' $script:deniedNote }" in inference
    done = desktop.split("        'done' {")[1].split("        'cancelled' {")[0]
    assert "$b.deniedNote" in done
    # For the KV cache match the model's real text goes to the history, not the note.
    assert "[void]$chatMessages.Add(@{ role = 'assistant'; content = [string]$jobEvent.text })" in done


def test_denial_note_is_added_to_the_request_copy_only():
    """After a denial an explicit note is added to the model call; it is not written to the persistent
    history ($messages), so the KV cache prefix is not broken. Because the note can be merged with
    the web-failure note it goes into $systemNotes and is written to the request copy ([System note])."""
    inference = read_text("agent/qwen-inference.ps1")
    assert "$systemNotes += (" in inference
    assert "DENIED" in inference and "do not guess the reason" in inference
    assert "$body.messages = @($sendMessages) + @(@{ role = 'user'; content = ('[System note] '" in inference
    assert "foreach ($m in @($systemNotes)) { [void]$messages.Add($m) }" not in inference
