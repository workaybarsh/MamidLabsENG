"""DNS rebinding protection for agent/qwen-agent-tools.py.

The IP addresses resolved while validating a URL are pinned for the connection.
Otherwise the real connection resolves the host again, and a malicious low-TTL record
could return a public IP during validation and a private/local IP for the connection.
For a pinned host getaddrinfo returns only the validated addresses; other hosts use
normal DNS.
"""
import importlib.util
import ipaddress
import socket
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _has_internet() -> bool:
    try:
        socket.create_connection(("example.com", 443), timeout=3).close()
        return True
    except OSError:
        return False


requires_internet = pytest.mark.skipif(not _has_internet(), reason="no internet access on this machine")


@pytest.fixture
def qat(monkeypatch):
    """Loads qwen-agent-tools.py from its file path instead of importing it (because of
    the hyphenated file name). After each test socket.getaddrinfo and the pin table are
    restored to the real module state; since the module changes socket.getaddrinfo at
    import time, this prevents leaks between tests."""
    real_getaddrinfo = socket.getaddrinfo
    spec = importlib.util.spec_from_file_location("qat_under_test", ROOT / "agent" / "qwen-agent-tools.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        yield module
    finally:
        socket.getaddrinfo = real_getaddrinfo


def test_module_patches_getaddrinfo_at_import_time(qat):
    assert socket.getaddrinfo.__name__ == "_patched_getaddrinfo"


@requires_internet
def test_validating_a_hostname_pins_its_resolved_addresses(qat):
    qat._pinned_addresses.clear()
    qat._validate_remote_url("https://example.com/page")

    assert "example.com" in qat._pinned_addresses
    pinned = qat._pinned_addresses["example.com"]
    assert pinned, "the pinned address list must not be empty"


@requires_internet
def test_pinned_host_getaddrinfo_call_returns_exactly_the_validated_addresses(qat):
    """This is EXACTLY the same function call urllib/http.client will make when
    opening the real TCP connection (inside socket.create_connection). The result being
    identical to the pinned one proves that the DNS gap between validation and
    connection is closed."""
    qat._pinned_addresses.clear()
    qat._validate_remote_url("https://example.com/page")

    result = socket.getaddrinfo("example.com", 443, type=socket.SOCK_STREAM)
    assert result == qat._pinned_addresses["example.com"]


@requires_internet
def test_unrelated_hosts_still_use_real_dns(qat):
    """Pinning must only affect validated hosts; the own DNS resolution of other
    libraries such as DDGS must not break."""
    qat._pinned_addresses.clear()
    qat._validate_remote_url("https://example.com/page")

    result = socket.getaddrinfo("one.one.one.one", 443, type=socket.SOCK_STREAM)
    assert result, "real DNS must work for a host that is not pinned"
    assert "one.one.one.one" not in qat._pinned_addresses


def test_private_and_link_local_addresses_are_still_rejected(qat):
    qat._pinned_addresses.clear()
    for url in (
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata, a classic SSRF target
        "http://10.0.0.1/",
        "http://192.168.1.1/",
    ):
        with pytest.raises(ValueError, match="Access to local, private or reserved"):
            qat._validate_remote_url(url)


def test_localhost_is_rejected_before_any_dns_pinning(qat):
    qat._pinned_addresses.clear()
    with pytest.raises(ValueError):
        qat._validate_remote_url("http://127.0.0.1:8080/health")
    # The port check runs before DNS; 127.0.0.1 must never be pinned.
    assert "127.0.0.1" not in qat._pinned_addresses


@requires_internet
def test_redirect_handler_pins_the_new_host_too(qat):
    """Every step in the redirect chain must connect to its own pinned address;
    otherwise the rebinding protection covers only the first host."""
    qat._pinned_addresses.clear()
    handler = qat._SafeRedirectHandler()

    class FakeRequest:
        def get_full_url(self):
            return "https://example.com/"

        full_url = "https://example.com/"
        headers = {}
        unverifiable = True

        def get_method(self):
            return "GET"

    try:
        handler.redirect_request(FakeRequest(), None, 302, "Found", {}, "https://example.com/next")
    except Exception:
        pass  # HTTPRedirectHandler's internal logic may fail with missing fields; what matters is the pinning.
    assert "example.com" in qat._pinned_addresses
