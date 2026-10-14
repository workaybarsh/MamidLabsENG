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


def test_web_research_turn_does_not_offer_generation_tools():
    """On a web-research (current-info) turn the image/video/document/chart tools are not
    offered unless explicitly requested; the model cannot enter those jobs."""
    worker = (ROOT / "agent" / "qwen-inference.ps1").read_text(encoding="utf-8-sig")
    assert "if (Test-CurrentInfoIntent $userText) {" in worker
    assert "$defs = @($defs | Where-Object { $_.function.name -notin @('generate_image', 'generate_video', 'create_document', 'generate_chart') })" in worker
    assert "Test-DocumentRequestIntent $userText" in worker
    assert "Test-ChartRequestIntent $userText" in worker


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
    assert "FABRICATED" not in got["offline"] and "without an internet connection" in got["offline"]
    assert "FABRICATED" not in got["webfail"] and "web search failed" in got["webfail"]
    assert got["normal"] == "cats are mammals"
    assert "No answer was produced" in got["empty"]
    assert "chart.png" in got["empty_file"]


def test_strict_fabrication_guard_strips_citations_and_blocks_current_info(tmp_path):
    """Strict anti-fabrication: if the web was TRIED but never succeeded, (a) a
    current-info question's model text is not shown, (b) for other answers fabricated
    'Source:'/URL lines are stripped."""
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh not found")
    desktop = (ROOT / "agent" / "qwen-inference.ps1").read_text(encoding="utf-8-sig")
    defs = "\n".join(
        extract_function(desktop, n)
        for n in ("Test-CurrentInfoIntent", "Add-WebFailureNote", "Remove-FabricatedCitations", "Get-FinalAnswer")
    )
    cur = _ps_quote("What is the latest iPhone model?")
    other = _ps_quote("How do I reverse a list in Python?")
    body = (
        "$ErrorActionPreference = 'Stop'\n" + defs + "\n"
        "$script:lastProducedFile = ''\n"
        "$r = [ordered]@{}\n"
        "$script:webToolFailCount = 1; $script:webToolAttempted = $true; $script:webToolSuccess = $false\n"
        f"$r['cur_fail'] = Get-FinalAnswer 'FABRICATED iPhone 15 Pro' {cur} $true\n"
        f"$r['strip'] = Get-FinalAnswer \"Short: reversed().`nSource: https://example.com/x`nMore: https://foo.bar\" {other} $true\n"
        "$script:webToolFailCount = 0; $script:webToolAttempted = $false; $script:webToolSuccess = $false\n"
        f"$r['no_web'] = Get-FinalAnswer 'See https://docs.python.org/3/' {other} $true\n"
        "$script:webToolAttempted = $true; $script:webToolSuccess = $true\n"
        f"$r['web_ok'] = Get-FinalAnswer 'Result: iPhone 16. Source: https://apple.com' {cur} $true\n"
        "$r | ConvertTo-Json -Compress\n"
    )
    script = tmp_path / "strict.ps1"
    script.write_text(body, encoding="utf-8-sig")
    proc = subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-File", str(script)],
                          capture_output=True, encoding="utf-8", errors="replace", check=False)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    assert "FABRICATED" not in got["cur_fail"] and "without an internet connection" in got["cur_fail"]
    assert "example.com" not in got["strip"] and "foo.bar" not in got["strip"]
    assert "reversed()" in got["strip"]
    assert "docs.python.org" in got["no_web"]
    assert "apple.com" in got["web_ok"] and "iPhone 16" in got["web_ok"]


def test_friendly_error_message_maps_raw_dotnet(tmp_path):
    """Raw .NET/HTTP messages (e.g. 'The response ended prematurely.') are shown to the
    user as a plain English message; an unrecognized message is returned unchanged."""
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh not found")
    desktop = (ROOT / "agent" / "qwen-inference.ps1").read_text(encoding="utf-8-sig")
    fn = extract_function(desktop, "Get-FriendlyErrorMessage")
    raw = _ps_quote("The response ended prematurely. (ResponseEnded)")
    unknown = _ps_quote("Some unknown error")
    body = (
        "$ErrorActionPreference = 'Stop'\n" + fn + "\n"
        "$r = [ordered]@{}\n"
        f"$r['premature'] = Get-FriendlyErrorMessage {raw}\n"
        f"$r['unknown'] = Get-FriendlyErrorMessage {unknown}\n"
        f"$r['empty'] = Get-FriendlyErrorMessage ''\n"
        "$r | ConvertTo-Json -Compress\n"
    )
    script = tmp_path / "friendly.ps1"
    script.write_text(body, encoding="utf-8-sig")
    proc = subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-File", str(script)],
                          capture_output=True, encoding="utf-8", errors="replace", check=False)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    assert "cut off" in got["premature"] and "prematurely" not in got["premature"]
    assert got["unknown"] == "Some unknown error"
    assert "unexpected" in got["empty"].lower()
    # A drop with only thinking accumulated is not an error (goes to the recovery path).
    worker = (ROOT / "agent" / "qwen-inference.ps1").read_text(encoding="utf-8-sig")
    assert "$usable = $result.finishReason -or ![string]::IsNullOrWhiteSpace($result.content) -or $result.toolParts.Count -gt 0 -or ![string]::IsNullOrWhiteSpace($result.reasoning)" in worker
    assert "Get-FriendlyErrorMessage $cause.Message" in worker
