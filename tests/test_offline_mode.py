"""Offline behavior tests.

User story: the internet chip looks enabled while the network drops (phone
hotspot, DNS timeout) and the model calls a web tool. The tool layer must not
crash; it must return clean JSON with an error, and the agent flow must count
the turn as complete instead of appending "[Error]" under the answer (stream
side: test_stream_watchdog.py).

This scenario is what fed the recurring "An existing connection was forcibly
closed by the remote host" (WSAECONNRESET) error on a real machine: an offline
web tool stalls for a long time, leaving the local model connection idle until
it is dropped. Here we guarantee the tool layer behaves cleanly while offline.
"""
import importlib.util
import json
import os
import socket
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def qat():
    """Loads qwen-agent-tools.py from its file path; it patches socket.getaddrinfo
    at import time, so the real one is restored afterwards (same pattern as
    test_dns_rebinding_fix)."""
    real_getaddrinfo = socket.getaddrinfo
    spec = importlib.util.spec_from_file_location("qat_offline", ROOT / "agent" / "qwen-agent-tools.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    try:
        yield module
    finally:
        socket.getaddrinfo = real_getaddrinfo


class _OfflineDDGS:
    """Stand-in for the connection error ddgs raises when DuckDuckGo is
    unreachable (the os error 10061 / ConnectError seen on a real machine)."""

    def __init__(self, *args, **kwargs):
        pass

    def text(self, *args, **kwargs):
        raise OSError("ConnectError: error sending request for url (os error 10061)")

    def images(self, *args, **kwargs):
        raise OSError("ConnectError: error sending request for url (os error 10061)")


def _install_offline_ddgs(monkeypatch):
    fake = types.ModuleType("ddgs")
    fake.DDGS = _OfflineDDGS
    monkeypatch.setitem(sys.modules, "ddgs", fake)


def test_offline_web_search_returns_clean_error_json(qat, monkeypatch):
    """With no network, web search does not raise; it returns valid error JSON the
    model can see (otherwise the tool call would break the loop)."""
    _install_offline_ddgs(monkeypatch)

    out = qat.search_web({"query": "einstein riddle", "count": 5})

    data = json.loads(out)  # valid JSON instead of an exception
    assert "error" in data
    assert "failed" in data["error"]


def test_offline_image_search_returns_clean_error_json(qat, monkeypatch):
    """Reference image search must also return error JSON while offline."""
    monkeypatch.setattr(os, "makedirs", lambda *a, **k: None)  # do not create folders in the repo
    _install_offline_ddgs(monkeypatch)

    out = qat.find_reference_image({"query": "istanbul"})

    data = json.loads(out)
    assert "error" in data
    assert "failed" in data["error"]


def test_offline_fetch_page_returns_clean_error_json(qat, monkeypatch):
    """When page download fails (black-holed connection) the tool returns clean error JSON."""
    # Avoid a DNS lookup in validation; the real connection is what fails here.
    monkeypatch.setattr(qat, "_validate_remote_url", lambda url: url)

    def boom(*args, **kwargs):
        raise OSError("connection could not be established (offline)")

    monkeypatch.setattr(qat, "_open_remote_url", boom)

    out = qat.fetch_page({"url": "https://example.com/page"})

    data = json.loads(out)
    assert "error" in data


def _python_with_ddgs():
    """Finds a Python that can import ddgs (including the installed venv\\app)."""
    candidates = [sys.executable]
    venv = Path(os.path.expandvars(r"%LOCALAPPDATA%\Programs\MamidLabs\venv\app\Scripts\python.exe"))
    if venv.is_file():
        candidates.append(str(venv))
    for exe in candidates:
        try:
            probe = subprocess.run([exe, "-c", "import ddgs"], capture_output=True, timeout=40)
            if probe.returncode == 0:
                return exe
        except Exception:
            continue
    return None


def test_offline_real_tool_process_fails_cleanly():
    """Runs the tool in a REAL subprocess behind a black-hole proxy: it must return
    valid error JSON without crashing (exit 0). Skipped when ddgs is unavailable."""
    exe = _python_with_ddgs()
    if not exe:
        pytest.skip("no Python with ddgs found")

    env = dict(os.environ)
    env["HTTP_PROXY"] = "http://127.0.0.1:9"
    env["HTTPS_PROXY"] = "http://127.0.0.1:9"
    payload = json.dumps({"action": "search_web", "query": "offline test", "count": 2})

    proc = subprocess.run(
        [exe, str(ROOT / "agent" / "qwen-agent-tools.py")],
        input=payload, capture_output=True, text=True, encoding="utf-8",
        timeout=150, env=env,
    )

    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    assert "error" in data
