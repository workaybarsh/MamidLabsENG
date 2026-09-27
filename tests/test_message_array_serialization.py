"""A real regression: a single-message chat turn with no tools blew up with a 400
error (a side effect of the Amp #28 fix).

In qwen-inference.ps1 $body.messages had been assigned DIRECTLY as a hashtable value
in the form "if (...) {...} else {...}". In PowerShell, when the output of an if/else
expression is captured into a hashtable value or a variable this way, a SINGLE-ELEMENT
array is "unwrapped" into a single object (array unwrapping); ConvertTo-Json writes it
as a plain object {...} instead of a JSON array [...]. In that case llama-server
returns 400 with "Expected 'messages' to be an array".

Caught on a real machine: in a new chat (a single user message), with no tools
enabled (agent/internet/office/documentCreate all off, toolTrustGuard = $null) the
request always failed - this was the most common real usage scenario (opening a new
chat).

This test actually runs PowerShell and proves the bug and the fix directly
(it does not just search the source text).
"""
import json
import shutil
import subprocess

import pytest


def run_pwsh(script: str) -> str:
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")
    proc = subprocess.run(
        [pwsh, "-NoProfile", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    if proc.returncode != 0:
        raise AssertionError(f"pwsh failed: {proc.stderr}")
    return proc.stdout.strip()


def test_broken_pattern_collapses_a_single_element_array():
    """First documents what is broken: a direct if/else assignment turns a
    single-element array into a plain object in ConvertTo-Json."""
    out = run_pwsh(r"""
$messages = [Collections.ArrayList]::new()
[void]$messages.Add(@{ role = 'user'; content = 'hello' })
$toolTrustGuard = $null
$body = @{ messages = if ($toolTrustGuard) { @($toolTrustGuard) + @($messages.ToArray()) } else { @($messages.ToArray()) } }
Write-Output ($body | ConvertTo-Json -Compress -Depth 10)
""")
    parsed = json.loads(out)
    assert not isinstance(parsed["messages"], list), (
        "This test documents the 'broken pattern'; if it now comes back as an array, "
        "PowerShell's behavior may have changed - the test should be updated."
    )


def test_array_cast_pattern_keeps_a_single_element_as_a_json_array():
    """The real fix: assigning to a separate variable first with an [array] cast keeps
    the JSON array even with a single element."""
    out = run_pwsh(r"""
$messages = [Collections.ArrayList]::new()
[void]$messages.Add(@{ role = 'user'; content = 'hello' })
$toolTrustGuard = $null
[array]$sendMessages = if ($toolTrustGuard) { @($toolTrustGuard) + @($messages.ToArray()) } else { @($messages.ToArray()) }
$body = @{ messages = $sendMessages }
Write-Output ($body | ConvertTo-Json -Compress -Depth 10)
""")
    parsed = json.loads(out)
    assert isinstance(parsed["messages"], list)
    assert len(parsed["messages"]) == 1
    assert parsed["messages"][0]["content"] == "hello"


def test_array_cast_pattern_also_works_with_multiple_elements():
    out = run_pwsh(r"""
$messages = [Collections.ArrayList]::new()
[void]$messages.Add(@{ role = 'user'; content = 'hello' })
[void]$messages.Add(@{ role = 'assistant'; content = 'hi' })
$toolTrustGuard = $null
[array]$sendMessages = if ($toolTrustGuard) { @($toolTrustGuard) + @($messages.ToArray()) } else { @($messages.ToArray()) }
$body = @{ messages = $sendMessages }
Write-Output ($body | ConvertTo-Json -Compress -Depth 10)
""")
    parsed = json.loads(out)
    assert isinstance(parsed["messages"], list)
    assert len(parsed["messages"]) == 2
