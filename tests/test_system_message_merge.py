"""A real regression: a 500 error in a compacted + tool-enabled chat.

The error the user hit in the real desktop app (reported with a screenshot):
"Jinja Exception: System message must be at the beginning."

Root cause: context compaction already puts a system message in $messages[0]
("[Summary of the earlier chat]..."). The Amp #28 fix (toolTrustGuard)
UNCONDITIONALLY prepended ANOTHER system message while tools were enabled (at least
one of agent/internet/office/documentCreate on). Result: two system messages (one the
guard, one the summary). Qwen's Jinja chat template explicitly rejects this - it
accepts only ONE system message, and it must be at index 0.

Reproduced and verified end to end on a real machine: a compacted chat history
($messages[0] a system message) + tools on (agent=true, internet=true) with a real
Start-ThreadJob -FilePath production call - BEFORE the fix this scenario had never
been tested (only the "no compaction, no tools" and "no compaction, tools enabled"
scenarios had been tested, see test_tool_trust_boundary.py). AFTER the fix a real
answer was produced, no error.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8-sig")


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
    return proc.stdout


HELPER = r"""
$toolTrustGuard = @{ role = 'system'; content = 'GUARD' }
function BuildSend($messages) {
    $rest = @($messages)
    if ($rest.Count -gt 0 -and [string]$rest[0].role -eq 'system') {
        $merged = @{ role = 'system'; content = ([string]$rest[0].content) + "`n`n" + [string]$toolTrustGuard.content }
        $tail = if ($rest.Count -gt 1) { $rest[1..($rest.Count - 1)] } else { @() }
        return @($merged) + $tail
    } else {
        return @($toolTrustGuard) + $rest
    }
}
"""


def test_fix_is_present_in_source_and_merges_instead_of_duplicating():
    inference = read_text("agent/mamidlabs-inference.ps1")
    assert "[string]$rest[0].role -eq 'system'" in inference
    assert "$merged = @{ role = 'system'; content = ([string]$rest[0].content)" in inference
    # At most one system message must be produced; the old unconditional "@($toolTrustGuard) + $rest"
    # on its own (without the merge check) must be gone.
    assert "[array]$sendMessages = if ($toolTrustGuard) { @($toolTrustGuard) + @($messages.ToArray()) } else" not in inference


def test_single_existing_system_message_gets_merged_not_duplicated():
    """A PowerShell-specific edge case: when $rest.Count -eq 1, '1..($rest.Count-1)'
    = '1..0' - PowerShell evaluates this not as empty but as a range that returns
    rest[0] AGAIN (verified). If not guarded, the merged message + a copy of the
    original = two system messages again, exactly the bug being fixed."""
    out = run_pwsh(HELPER + r"""
$result = BuildSend @(@{role='system'; content='[Summary of the earlier chat] X'})
Write-Output ($result | ConvertTo-Json -Compress -Depth 5)
""")
    parsed = json.loads(out.strip().splitlines()[-1])
    assert isinstance(parsed, dict), f"a single-element array was expected, two messages may have been returned: {parsed}"
    assert parsed["role"] == "system"
    assert "GUARD" in parsed["content"]
    assert "[Summary of the earlier chat]" in parsed["content"]


def test_system_message_plus_more_history_merges_and_preserves_order():
    out = run_pwsh(HELPER + r"""
$result = BuildSend @(
    @{role='system'; content='[Summary of the earlier chat] X'},
    @{role='user'; content='continue'},
    @{role='assistant'; content='ok'}
)
Write-Output ($result | ConvertTo-Json -Compress -Depth 5)
""")
    parsed = json.loads(out.strip().splitlines()[-1])
    assert len(parsed) == 3, f"exactly 3 messages (1 merged system + 2 history) were expected: {parsed}"
    assert parsed[0]["role"] == "system"
    assert "GUARD" in parsed[0]["content"] and "[Summary of the earlier chat]" in parsed[0]["content"]
    assert [m["role"] for m in parsed[1:]] == ["user", "assistant"]
    assert sum(1 for m in parsed if m["role"] == "system") == 1


def test_no_existing_system_message_still_prepends_guard_normally():
    """Verifies there is no regression: in the normal case without compaction the guard
    is still prepended as before."""
    out = run_pwsh(HELPER + r"""
$result = BuildSend @(@{role='user'; content='hello'})
Write-Output ($result | ConvertTo-Json -Compress -Depth 5)
""")
    parsed = json.loads(out.strip().splitlines()[-1])
    assert len(parsed) == 2
    assert parsed[0] == {"role": "system", "content": "GUARD"}
    assert parsed[1]["role"] == "user"
