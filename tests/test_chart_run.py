"""Dependency-free chart generator (agent\\chart-run.ps1) + tool wiring.

The chat tool `generate_chart` runs this script in a separate bundled-pwsh process;
the script reads a JSON spec from stdin and writes a JSON result to stdout. These tests
produce every chart type in a real pwsh and prove the safe expression evaluator does not
use Invoke-Expression. Skipped when PowerShell 7 is unavailable (see conftest.py).
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "agent" / "chart-run.ps1"
INFERENCE = ROOT / "agent" / "mamidlabs-inference.ps1"
DESKTOP = ROOT / "mamidlabs-desktop.ps1"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def pwsh_exe():
    exe = shutil.which("pwsh")
    if not exe:
        pytest.skip("PowerShell 7 is unavailable")
    return exe


def run_chart(spec: dict) -> dict:
    """Run chart-run.ps1 and return the JSON result as a dict."""
    spec = dict(spec)
    if spec.get("out"):
        spec["out"] = str(spec["out"])
    proc = subprocess.run(
        [pwsh_exe(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(CHART)],
        input=json.dumps(spec),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert proc.stdout.strip(), f"no output; stderr={proc.stderr!r}"
    return json.loads(proc.stdout)


def assert_png(result: dict, tmp_path: Path) -> None:
    assert "error" not in result, result
    out = Path(result["path"])
    assert out.is_file() and out.suffix == ".png", result
    assert out.stat().st_size > 0
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", "not a valid PNG signature"


def test_bar_with_top_level_values(tmp_path):
    result = run_chart({
        "type": "bar",
        "labels": ["Jan", "Feb", "Mar", "Apr", "May"],
        "values": [12, 19, 8, 15, 22],
        "title": "Monthly sales",
        "ylabel": "Count",
        "out": tmp_path / "bar.png",
    })
    assert_png(result, tmp_path)
    assert result["type"] == "bar"


def test_line_with_multiple_series(tmp_path):
    result = run_chart({
        "type": "line",
        "labels": ["2020", "2021", "2022", "2023", "2024"],
        "series": [
            {"name": "TR", "values": [10, 14, 13, 18, 21]},
            {"name": "ENG", "values": [8, 9, 12, 14, 19]},
        ],
        "title": "Yearly growth",
        "out": tmp_path / "line.png",
    })
    assert_png(result, tmp_path)
    assert result["type"] == "line"


def test_pie_with_top_level_values(tmp_path):
    result = run_chart({
        "type": "pie",
        "labels": ["Apple", "Pear", "Banana", "Cherry"],
        "values": [35, 25, 20, 20],
        "title": "Fruit share",
        "out": tmp_path / "pie.png",
    })
    assert_png(result, tmp_path)
    assert result["type"] == "pie"


def test_pie_rejects_non_positive_values(tmp_path):
    result = run_chart({
        "type": "pie",
        "labels": ["A", "B"],
        "values": [0, 0],
        "out": tmp_path / "pie0.png",
    })
    assert "error" in result


def test_scatter_accepts_objects_and_arrays(tmp_path):
    obj = run_chart({
        "type": "scatter",
        "series": [{"name": "Group A", "points": [{"x": 1, "y": 2}, {"x": 2, "y": 3.5}, {"x": 3, "y": 3}]}],
        "out": tmp_path / "scatter-obj.png",
    })
    assert_png(obj, tmp_path)
    arr = run_chart({
        "type": "scatter",
        "series": [{"name": "Group A", "points": [[1, 2], [2, 3.5], [3, 3]]}],
        "out": tmp_path / "scatter-arr.png",
    })
    assert_png(arr, tmp_path)


def test_function_curves_sin_cos_and_power(tmp_path):
    result = run_chart({
        "type": "function",
        "functions": ["sin(x)", "cos(x)", "x^2"],
        "xmin": -6.283185307179586,
        "xmax": 6.283185307179586,
        "samples": 800,
        "title": "Function curve",
        "out": tmp_path / "fn.png",
    })
    assert_png(result, tmp_path)
    assert result["type"] == "function"


def test_function_invalid_expression_reports_error(tmp_path):
    result = run_chart({
        "type": "function",
        "functions": ["unknownfn(x)"],
        "xmin": 0,
        "xmax": 1,
        "out": tmp_path / "bad-fn.png",
    })
    assert "error" in result


def test_function_rejects_arbitrary_code(tmp_path):
    result = run_chart({
        "type": "function",
        "functions": ["x); Remove-Item C:\\ ; ("],
        "xmin": 0,
        "xmax": 1,
        "out": tmp_path / "evil.png",
    })
    assert "error" in result


def test_unknown_type_reports_error(tmp_path):
    result = run_chart({"type": "wat", "labels": ["A"], "values": [1], "out": tmp_path / "x.png"})
    assert "error" in result


def test_missing_out_reports_error(tmp_path):
    result = run_chart({"type": "bar", "labels": ["A"], "values": [1]})
    assert "error" in result


def test_source_uses_no_invoke_expression():
    import re
    src = read_text(CHART)
    # Looks for a real call at the start of a line, not the mention in a comment.
    assert not re.search(r"(?m)^\s*(Invoke-Expression|iex)\b", src), \
        "the safe evaluator must not use Invoke-Expression"


def test_inference_wires_generate_chart_tool():
    src = read_text(INFERENCE)
    assert "name = 'generate_chart'" in src, "tool definition missing"
    assert "$job.chartgen" in src, "tool not offered via the chartgen flag"
    assert "$name -eq 'generate_chart'" in src, "tool call not handled"
    assert "chart-run.ps1" in src, "chart script not invoked"
    assert "chartFile" in src, "chart event not emitted"


def test_desktop_handles_chart_event_and_flag():
    src = read_text(DESKTOP)
    assert "chartgen = $true" in src, "jobData does not send chartgen"
    assert "'chart'" in src, "UI does not handle the chart event"
    assert "generate_chart" in src, "tool label/system prompt missing"


def test_smoke_test_has_chart_step():
    src = read_text(ROOT / "scripts" / "smoke-test.ps1")
    assert "[switch]$SkipChart" in src, "chart step must be skippable"
    assert "Write-Section 'Chart generation" in src, "chart step missing"
    assert "Report 'chart'" in src, "chart step not reported"
    assert "chart-run.ps1" in src, "chart script not invoked in the smoke test"


def _extract_function(source: str, name: str) -> str:
    import re
    m = re.search(r"function " + re.escape(name) + r"\b[\s\S]*?\n\}", source)
    assert m, f"{name} not found"
    return m.group(0)


def test_invoke_chart_tool_creates_png(tmp_path):
    """Invoke-ChartTool inside the worker must produce a PNG with the real chart-run.ps1."""
    pwsh = pwsh_exe()
    src = read_text(INFERENCE)
    fns = _extract_function(src, "Get-InstallRoot") + "\n" + _extract_function(src, "Invoke-ChartTool")
    (tmp_path / "agent").mkdir()
    shutil.copy(CHART, tmp_path / "agent" / "chart-run.ps1")

    template = """
$JobData = @{ root = '__ROOT__' }
$o = '{"type":"bar","labels":["A","B","C"],"values":[3,7,4],"title":"Test chart"}' | ConvertFrom-Json
$r = Invoke-ChartTool $o
Write-Output $r
Write-Output ('ARTIFACT=' + [string]$script:createdArtifact)
"""
    script = fns + "\n" + template.replace("__ROOT__", str(tmp_path))
    proc = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Chart created" in proc.stdout, proc.stdout + proc.stderr
    pngs = list((tmp_path / "outputs" / "charts").glob("*.png"))
    assert pngs and pngs[0].stat().st_size > 0


def test_invoke_chart_tool_rejects_bad_type(tmp_path):
    pwsh = pwsh_exe()
    src = read_text(INFERENCE)
    fns = _extract_function(src, "Get-InstallRoot") + "\n" + _extract_function(src, "Invoke-ChartTool")
    (tmp_path / "agent").mkdir()
    shutil.copy(CHART, tmp_path / "agent" / "chart-run.ps1")
    template = """
$JobData = @{ root = '__ROOT__' }
$o = '{"type":"wat","values":[1]}' | ConvertFrom-Json
Write-Output (Invoke-ChartTool $o)
"""
    script = fns + "\n" + template.replace("__ROOT__", str(tmp_path))
    proc = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Invalid chart type" in proc.stdout
