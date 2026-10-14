import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def extract_function(source: str, name: str) -> str:
    start = source.index(f"function {name}")
    end = source.find("\nfunction ", start + 1)
    return source[start:end if end != -1 else len(source)]


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def test_intent_helpers_classify_prompts(tmp_path):
    """Document/video/chart intent helpers: if the user did not explicitly ask,
    the tool is NOT executed. The classification is verified in a real PowerShell."""
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh not found")
    desktop = (ROOT / "agent" / "qwen-inference.ps1").read_text(encoding="utf-8-sig")
    defs = "\n".join(
        extract_function(desktop, name)
        for name in ("Test-DocumentRequestIntent", "Test-VideoRequestIntent", "Test-ChartRequestIntent")
    )
    cases = [
        ("doc_pdf", "Test-DocumentRequestIntent $T", "create a PDF report", True),
        ("doc_word", "Test-DocumentRequestIntent $T", "prepare a word document", True),
        ("doc_pres", "Test-DocumentRequestIntent $T", "make a presentation", True),
        ("doc_report", "Test-DocumentRequestIntent $T", "write a short report and save it", True),
        ("doc_neg_info", "Test-DocumentRequestIntent $T", "give me brief info about cats", False),
        ("doc_neg_read", "Test-DocumentRequestIntent $T", "read this document and summarize it", False),
        ("doc_neg_math", "Test-DocumentRequestIntent $T", "what is 2 + 2?", False),
        ("vid_generate", "Test-VideoRequestIntent $T $false", "generate a short video", True),
        ("vid_make", "Test-VideoRequestIntent $T $false", "make a video of a cat", True),
        ("vid_anim", "Test-VideoRequestIntent $T $false", "create an animation", True),
        ("vid_neg_info", "Test-VideoRequestIntent $T $false", "tell me about cats", False),
        ("vid_neg_q", "Test-VideoRequestIntent $T $false", "what happens in this video?", False),
        ("vid_ctx", "Test-VideoRequestIntent $T $true", "continue this video", True),
        ("chart_draw", "Test-ChartRequestIntent $T", "draw a chart of the population", True),
        ("chart_bar", "Test-ChartRequestIntent $T", "create a bar chart", True),
        ("chart_plot", "Test-ChartRequestIntent $T", "plot sin(x) from 0 to 10", True),
        ("chart_neg_info", "Test-ChartRequestIntent $T", "tell me about cats", False),
        ("chart_neg_math", "Test-ChartRequestIntent $T", "what is 5 factorial?", False),
    ]
    lines = ["$ErrorActionPreference = 'Stop'", defs, "$T = ''", "$res = [ordered]@{}"]
    for key, call, prompt, _ in cases:
        lines.append(f"$T = {_ps_quote(prompt)}")
        lines.append(f"$res['{key}'] = [bool]({call})")
    lines.append("$res | ConvertTo-Json -Compress")
    script = tmp_path / "intent.ps1"
    script.write_text("\n".join(lines), encoding="utf-8-sig")
    proc = subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-File", str(script)],
                          capture_output=True, encoding="utf-8", errors="replace", check=False)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    for key, _, prompt, expected in cases:
        assert got[key] is expected, f"{key} ({prompt!r}) = {got[key]}, expected {expected}"


def test_worker_refuses_unprompted_generation_calls():
    """In the tool loop an unprompted create_document/generate_video/generate_chart
    is not executed (the same guard generate_image already had)."""
    worker = (ROOT / "agent" / "qwen-inference.ps1").read_text(encoding="utf-8-sig")
    assert "function Test-DocumentRequestIntent" in worker
    assert "function Test-VideoRequestIntent" in worker
    assert "function Test-ChartRequestIntent" in worker
    assert "$name -eq 'generate_video' -and !(Test-VideoRequestIntent $userText" in worker
    assert "$name -eq 'create_document' -and !(Test-DocumentRequestIntent $userText)" in worker
    assert "$name -eq 'generate_chart' -and !(Test-ChartRequestIntent $userText)" in worker
    assert "if ($videoCall -and !(Test-VideoRequestIntent $userText" in worker


def test_web_failure_note_is_appended(tmp_path):
    """When a web tool failed and the answer does not say so, an honest note is
    appended; when it already says so, the answer is unchanged."""
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh not found")
    desktop = (ROOT / "agent" / "qwen-inference.ps1").read_text(encoding="utf-8-sig")
    fn = extract_function(desktop, "Add-WebFailureNote")
    body = (
        "$ErrorActionPreference = 'Stop'\n" + fn + "\n"
        "$r = [ordered]@{}\n"
        "$script:webToolFailCount = 0; $r['none'] = Add-WebFailureNote 'hello'\n"
        "$script:webToolFailCount = 1; $r['plain'] = Add-WebFailureNote 'hello'\n"
        "$script:webToolFailCount = 1; $r['ack'] = Add-WebFailureNote 'The web search failed.'\n"
        "$r | ConvertTo-Json -Compress\n"
    )
    script = tmp_path / "note.ps1"
    script.write_text(body, encoding="utf-8-sig")
    proc = subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-File", str(script)],
                          capture_output=True, encoding="utf-8", errors="replace", check=False)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    assert got["none"] == "hello"
    assert "The web search failed" in got["plain"]
    assert got["ack"] == "The web search failed."


def test_document_tool_requires_a_document_context():
    """read_document is offered only with an attached document or a file intent (the
    small model would invent a non-existent file and call it)."""
    worker = (ROOT / "agent" / "qwen-inference.ps1").read_text(encoding="utf-8-sig")
    assert "function Add-WebFailureNote" in worker
    assert "function Get-FinalAnswer" in worker
    assert "Emit 'done' (Get-FinalAnswer $final $userText ([bool]$JobData.internet)) $lastUsage $toolExchange" in worker
    assert "$hasDocumentContext = @($script:attachedDocuments).Count -gt 0 -or (Test-FileIntent $userText)" in worker
    assert "if ($hasDocumentContext) {" in worker
    # Repeat-loop hardening.
    assert "$script:completedTools = @{}" in worker
    assert "$offeredTools = @($offeredTools | Where-Object { -not $script:completedTools.ContainsKey($_.function.name) })" in worker


def test_final_answer_blocks_fabrication_and_empty(tmp_path):
    """With a current-information request and no web (search failed / Internet off) the
    model TEXT IS NOT USED (fabrication never reaches the screen); an empty answer is
    replaced by a clear message."""
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh not found")
    desktop = (ROOT / "agent" / "qwen-inference.ps1").read_text(encoding="utf-8-sig")
    defs = "\n".join(
        extract_function(desktop, n)
        for n in ("Test-CurrentInfoIntent", "Add-WebFailureNote", "Get-FinalAnswer")
    )
    cur = _ps_quote("What is the current weather in Istanbul? Give the latest info.")
    other = _ps_quote("Tell me about cats.")
    body = (
        "$ErrorActionPreference = 'Stop'\n" + defs + "\n"
        "$script:webToolFailCount = 0; $script:lastProducedFile = ''\n"
        "$r = [ordered]@{}\n"
        f"$r['offline'] = Get-FinalAnswer 'FABRICATED 21 degrees' {cur} $false\n"
        "$script:webToolFailCount = 2\n"
        f"$r['webfail'] = Get-FinalAnswer 'FABRICATED iPhone 15' {cur} $true\n"
        "$script:webToolFailCount = 0\n"
        f"$r['normal'] = Get-FinalAnswer 'cats are mammals' {other} $true\n"
        f"$r['empty'] = Get-FinalAnswer '' {other} $true\n"
        "$script:lastProducedFile = 'C:\\out\\chart.png'\n"
        f"$r['empty_file'] = Get-FinalAnswer '' {other} $true\n"
        "$r | ConvertTo-Json -Compress\n"
    )
    script = tmp_path / "final.ps1"
    script.write_text(body, encoding="utf-8-sig")
    proc = subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-File", str(script)],
                          capture_output=True, encoding="utf-8", errors="replace", check=False)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    assert "FABRICATED" not in got["offline"] and "Internet access is disabled" in got["offline"]
    assert "FABRICATED" not in got["webfail"] and "web search failed" in got["webfail"]
    assert got["normal"] == "cats are mammals"
    assert "No answer was produced" in got["empty"]
    assert "chart.png" in got["empty_file"]
