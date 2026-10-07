import json
import shutil
import socket
import struct
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


class FinishedButOpenHandler(BaseHTTPRequestHandler):
    """Ends the answer with finish_reason but does not send [DONE] and keeps the connection
    open (seen in a real UI test: the job waited for the ~5 min OS timeout)."""

    protocol_version = "HTTP/1.1"

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in (
            {"choices": [{"index": 0, "delta": {"content": "Hello"}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ):
            self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            self.wfile.flush()
        time.sleep(60)

    def log_message(self, *args):
        pass


def test_finished_stream_that_never_closes_does_not_hang(tmp_path):
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh not found")
    server = ThreadingHTTPServer(("127.0.0.1", 0), FinishedButOpenHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        script = tmp_path / "watchdog.ps1"
        script.write_text(
            f"""
$job = @{{ messages = @(@{{ role = 'user'; content = 'hello' }}); compactOnly = $true; provider = 'local'; model = 'test'; contextLimit = 32768; maxTokens = 100; agent = $false; internet = $false; office = $false; documentCreate = $false }}
$shared = @{{ Cancel = $false }}
. '{ROOT}\\agent\\qwen-inference.ps1' $job $shared | Out-Null
$endpoint = 'http://127.0.0.1:{server.server_port}/v1/chat/completions'
# On compactOnly exit the script closes its own HttpClient.
$script:http = [Net.Http.HttpClient]::new(); $script:http.Timeout = [Threading.Timeout]::InfiniteTimeSpan
$turn = @{{ reasoning = ''; content = ''; usage = $null; toolParts = @{{}}; finishReason = $null }}
$sw = [Diagnostics.Stopwatch]::StartNew()
Invoke-ChatCompletion @{{ messages = @() }} $shared $false $turn | Out-Null
@{{ content = $turn.content; finish = $turn.finishReason; seconds = $sw.Elapsed.TotalSeconds }} | ConvertTo-Json -Compress
""",
            encoding="utf-8-sig",
        )
        result = subprocess.run(
            [pwsh, "-NoProfile", "-NonInteractive", "-File", str(script)],
            capture_output=True, encoding="utf-8", errors="replace", check=False, timeout=90,
        )
    finally:
        server.shutdown()
    assert result.returncode == 0, result.stderr or result.stdout
    got = json.loads(result.stdout.strip().splitlines()[-1])
    assert got["content"] == "Hello"
    assert got["finish"] == "stop"
    assert got["seconds"] < 15, got


class DropConnectionMidStreamHandler(BaseHTTPRequestHandler):
    """Sends one content chunk and then closes the connection with an RST, without
    [DONE]/finish_reason. The local server can close a keep-alive connection that sits
    idle while a tool runs (web search, OCR, documents); when the client reused that
    stale connection the stream failed mid-answer with "An existing connection was
    forcibly closed by the remote host." (WSAECONNRESET). With answer text in hand this
    must no longer count as an error, and the UI must not append "[Error]" under the
    answer."""

    protocol_version = "HTTP/1.1"

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(
            b"data: "
            + json.dumps({"choices": [{"index": 0, "delta": {"content": "Hello"}}]}).encode()
            + b"\n\n"
        )
        self.wfile.flush()
        # A short wait so the content chunk reaches and is processed by the client; then
        # the connection is dropped with an RST, without [DONE]/finish_reason (as in the
        # real scenario: tokens are read first, then the connection dies).
        time.sleep(1.0)
        # SO_LINGER(1,0) makes close() send an RST: a realistic WSAECONNRESET stand-in.
        self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        try:
            self.connection.close()
        except OSError:
            pass
        self.close_connection = True

    def log_message(self, *args):
        pass


def test_dropped_stream_with_received_text_is_not_reported_as_error(tmp_path):
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh not found")
    server = ThreadingHTTPServer(("127.0.0.1", 0), DropConnectionMidStreamHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        script = tmp_path / "drop.ps1"
        script.write_text(
            f"""
$job = @{{ messages = @(@{{ role = 'user'; content = 'hello' }}); compactOnly = $true; provider = 'local'; model = 'test'; contextLimit = 32768; maxTokens = 100; agent = $false; internet = $false; office = $false; documentCreate = $false }}
$shared = @{{ Cancel = $false }}
. '{ROOT}\\agent\\qwen-inference.ps1' $job $shared | Out-Null
$endpoint = 'http://127.0.0.1:{server.server_port}/v1/chat/completions'
$script:http = [Net.Http.HttpClient]::new(); $script:http.Timeout = [Threading.Timeout]::InfiniteTimeSpan
$turn = @{{ reasoning = ''; content = ''; usage = $null; toolParts = @{{}}; finishReason = $null }}
$threw = $false
try {{
    Invoke-ChatCompletion @{{ messages = @() }} $shared $false $turn | Out-Null
}} catch {{
    $threw = [string]$_.Exception.GetBaseException().Message
}}
@{{ content = $turn.content; finish = $turn.finishReason; threw = $threw }} | ConvertTo-Json -Compress
""",
            encoding="utf-8-sig",
        )
        result = subprocess.run(
            [pwsh, "-NoProfile", "-NonInteractive", "-File", str(script)],
            capture_output=True, encoding="utf-8", errors="replace", check=False, timeout=60,
        )
    finally:
        server.shutdown()
    assert result.returncode == 0, result.stderr or result.stdout
    got = json.loads(result.stdout.strip().splitlines()[-1])
    # The received text is kept, the stream counts as complete and no exception escapes,
    # so the UI does not append "[Error] ... forcibly closed" under the answer.
    assert got["content"] == "Hello"
    assert got["finish"] == "connection-closed"
    assert got["threw"] is False

