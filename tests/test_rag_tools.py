"""Local RAG: BM25 search in the bound folder (`agent/rag-tools.py`).

So the model does not have to read every file to find the relevant parts in a
large folder there is a dictionary-based (BM25) local search; no extra package is
needed and it is fully offline. The index is kept under tools\\rag and is not
rebuilt as long as the file signature is unchanged. These tests run with a real
Python subprocess; the PowerShell side is checked statically.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "agent/rag-tools.py"


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8-sig")


def run_tool(payload: dict) -> dict:
    result = subprocess.run(
        [sys.executable, str(TOOL)],
        input=json.dumps(payload), capture_output=True, text=True,
        encoding="utf-8", timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def make_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "documents"
    (folder / "sub").mkdir(parents=True)
    (folder / "accounting.txt").write_text(
        "2026 budget report\n"
        "The accounting department reports expense items monthly.\n"
        "Rent, electricity and personnel are the main expense items.\n",
        encoding="utf-8",
    )
    (folder / "sub" / "software.md").write_text(
        "# Installation\n"
        "To install the server first install Python, then create a virtual environment.\n",
        encoding="utf-8",
    )
    (folder / "notes.log").write_text("meeting notes\n" * 40, encoding="utf-8")
    return folder


def make_store(tmp_path: Path) -> Path:
    return tmp_path / "rag" / "bound.json"


def test_search_ranks_the_relevant_chunk_first(tmp_path):
    folder = make_folder(tmp_path)
    store = make_store(tmp_path)
    answer = run_tool({"action": "search", "root": str(folder),
                       "query": "accounting expense report", "top": 3, "store": str(store)})
    assert answer["hits"], "a match must be found"
    assert answer["hits"][0]["path"] == "accounting.txt"
    assert "expense" in answer["hits"][0]["text"]
    assert answer["hits"][0]["line"] == 1
    assert store.is_file(), "the index must be kept under tools\\rag"
    assert answer["cached"] is False


def test_second_search_reuses_the_index(tmp_path):
    folder = make_folder(tmp_path)
    store = make_store(tmp_path)
    payload = {"action": "search", "root": str(folder),
               "query": "server installation", "top": 3, "store": str(store)}
    first = run_tool(payload)
    second = run_tool(payload)
    assert first["hits"][0]["path"].endswith("software.md")
    assert second["cached"] is True, "the index must not be rebuilt when nothing changed"


def test_index_is_rebuilt_when_a_file_changes(tmp_path):
    folder = make_folder(tmp_path)
    store = make_store(tmp_path)
    payload = {"action": "search", "root": str(folder), "query": "pacific", "top": 3,
               "store": str(store)}
    assert run_tool(payload)["hits"] == []
    (folder / "new.txt").write_text("The Pacific ocean is the largest ocean.\n", encoding="utf-8")
    answer = run_tool(payload)
    assert answer["cached"] is False
    assert answer["hits"][0]["path"] == "new.txt"


def test_binary_and_unknown_files_are_skipped(tmp_path):
    folder = tmp_path / "binary"
    folder.mkdir()
    (folder / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"ocean" * 10)
    (folder / "data.bin").write_bytes(b"\x00\x01ocean\x00\x02")
    (folder / "ocean.txt").write_text("ocean note\n", encoding="utf-8")
    answer = run_tool({"action": "search", "root": str(folder), "query": "ocean",
                       "top": 5, "store": str(make_store(tmp_path))})
    paths = [hit["path"] for hit in answer["hits"]]
    assert paths == ["ocean.txt"]


def test_heavy_directories_are_skipped(tmp_path):
    folder = tmp_path / "project"
    (folder / "node_modules" / "package").mkdir(parents=True)
    (folder / "venv" / "Scripts").mkdir(parents=True)
    (folder / "node_modules" / "package" / "index.js").write_text("ocean dependency\n", encoding="utf-8")
    (folder / "venv" / "Scripts" / "activate.ps1").write_text("ocean venv\n", encoding="utf-8")
    (folder / "source.py").write_text("# ocean project\n", encoding="utf-8")
    answer = run_tool({"action": "search", "root": str(folder), "query": "ocean",
                       "top": 5, "store": str(make_store(tmp_path))})
    paths = [hit["path"] for hit in answer["hits"]]
    assert paths == ["source.py"], paths


def test_missing_folder_and_empty_query_report_clearly(tmp_path):
    missing = run_tool({"action": "search", "root": str(tmp_path / "missing"),
                        "query": "x", "store": str(make_store(tmp_path))})
    assert "error" in missing
    folder = make_folder(tmp_path)
    empty = run_tool({"action": "search", "root": str(folder), "query": "   ",
                      "store": str(make_store(tmp_path))})
    assert empty["hits"] == []
    assert "term" in empty["message"]


def test_index_action_reports_counts(tmp_path):
    folder = make_folder(tmp_path)
    answer = run_tool({"action": "index", "root": str(folder), "store": str(make_store(tmp_path))})
    assert answer["files"] >= 3
    assert answer["chunks"] >= 3
    assert answer["cached"] is False


def test_corrupt_index_is_rebuilt(tmp_path):
    folder = make_folder(tmp_path)
    store = make_store(tmp_path)
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text("{corrupt json", encoding="utf-8")
    answer = run_tool({"action": "search", "root": str(folder), "query": "accounting",
                       "top": 3, "store": str(store)})
    assert answer["hits"], "a corrupt index must be ignored silently and rebuilt"


def test_inference_exposes_the_search_folder_tool():
    source = read_text("agent/mamidlabs-inference.ps1")
    assert "function Get-RagPyPath" in source
    assert "function Get-RagStorePath" in source
    assert "$defs += @{ type = 'function'; function = @{ name = 'search_folder';" in source
    # Same gate as the folder tools: not offered on an audio turn (unless a file is asked for).
    guard = source.index("$audioAttachedThisTurn = @($job.currentAudioPaths")
    tool_def = source.index("name = 'search_folder'")
    assert guard < tool_def
    branch = source.index("if ($name -eq 'search_folder')")
    assert "Get-RagStorePath $folder" in source[branch:branch + 1200]
    assert "Get-RagPyPath" in source[branch:branch + 1200]


def test_search_folder_result_is_labeled_untrusted():
    source = read_text("agent/mamidlabs-inference.ps1")
    label = source.index("$script:untrustedContentRead = $true")
    block = source[label - 300:label + 300]
    assert "'search_folder'" in block, "bound-folder content must be labelled untrusted"
