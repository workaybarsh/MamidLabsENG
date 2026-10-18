"""Automatic effort selection (MAX/FLASH).

MAX and FLASH use the same model files; the only difference is the server's
``--reasoning-effort`` value. Switching profiles reloads the model (~15 s), so
instead ``chat_template_kwargs.reasoning_effort`` is sent per request. The choice
is made ONCE at the start of a turn (from the difficulty of the last user
message) and stays constant for the turn (so the system prefix does not change).
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8-sig")


def extract_functions(source: str, names) -> str:
    out = []
    for name in names:
        match = re.search(r"function " + re.escape(name) + r"\b[\s\S]*?\n\}", source)
        assert match, f"{name} not found"
        out.append(match.group(0))
    return "\n".join(out)


def run_effort(samples) -> list[str]:
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")
    source = read_text("mamidlabs-desktop.ps1")
    prelude = "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
    parts = [prelude, extract_functions(source, ["Get-AutoReasoningEffort"])]
    for sample in samples:
        parts.append("Get-AutoReasoningEffort @'\n" + sample + "\n'@")
    script = "\n".join(parts)
    result = subprocess.run(
        [pwsh, "-NoProfile", "-Command", script],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def test_greeting_stays_low():
    assert run_effort(["hello", "hi, how are you?"]) == ["low", "low"]


def test_simple_short_question_stays_low():
    assert run_effort(["What's the weather today?"]) == ["low"]


def test_math_question_gets_high_effort():
    assert run_effort(["Solve the integral \u222b x^2 dx step by step and prove the result."]) == ["xhigh"]


def test_code_debug_request_gets_high_effort():
    sample = (
        "Debug the following Python code and explain why it is slow:\n"
        "```python\n"
        "def f(x):\n"
        "    return sum(i * i for i in range(x))\n"
        "```\n"
        "Also optimize the memory usage."
    )
    assert run_effort([sample]) == ["xhigh"]


def test_medium_length_analysis_gets_medium_or_higher():
    sample = "Analyze the following text and list the main ideas. " + ("Sentence. " * 60)
    assert run_effort([sample])[0] in {"medium", "xhigh"}


def test_effort_is_only_used_for_local_max_flash():
    source = read_text("mamidlabs-desktop.ps1")
    assert "Get-AutoReasoningEffort $effortText" in source
    assert (
        "if ($script:providerId -eq 'local' -and $script:autoEffort -and $profile -in @('max', 'flash'))"
        in source
    ), "automatic effort must be computed only for local MAX/FLASH"


def test_profiles_menu_toggles_auto_effort_and_persists():
    source = read_text("mamidlabs-desktop.ps1")
    assert "Automatic effort (by question)" in source
    assert "$script:apiSettings.autoEffort = $script:autoEffort" in source


def test_auto_effort_defaults_to_on():
    source = read_text("mamidlabs-desktop.ps1")
    assert "$script:autoEffort = $true" in source
