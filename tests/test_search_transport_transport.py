"""Tests for scripts/search_transport/transport.py -- the controlled
rollout guarantees this whole phase depends on:
  - default mode is direct
  - proxy mode only activates when explicitly configured via
    SEARCH_TRANSPORT=proxy, independent of TOR_ENABLED (see this
    repository's own actively-enforced invariant in
    tests/test_tor_production_dark_launch.py that no collection code path
    may depend on TOR_ENABLED)
  - a genuinely unreachable proxy raises rather than silently returning a
    direct-mode (no-proxy) transport
No real Tor process is required: the "reachable" cases bind a local
TCP listener as a stand-in SOCKS port.
"""
import socket
import sys
import threading
from contextlib import closing
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.search_transport.transport import (
    MODE_DIRECT,
    MODE_PROXY,
    ProxyTransportUnavailableError,
    get_search_transport,
)


def _listen_on_free_port():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    host, port = server.getsockname()

    def _accept_forever():
        try:
            while True:
                connection, _ = server.accept()
                connection.close()
        except OSError:
            pass

    thread = threading.Thread(target=_accept_forever, daemon=True)
    thread.start()

    return server, host, port


def test_default_mode_is_direct_when_unset(monkeypatch):
    monkeypatch.delenv("SEARCH_TRANSPORT", raising=False)

    transport = get_search_transport()

    assert transport.mode == MODE_DIRECT
    assert transport.playwright_proxy_config is None


def test_explicit_direct_mode(monkeypatch):
    monkeypatch.setenv("SEARCH_TRANSPORT", "direct")

    transport = get_search_transport()

    assert transport.mode == MODE_DIRECT
    assert transport.playwright_proxy_config is None


def test_direct_mode_timeouts_match_pre_phase_hardcoded_values(monkeypatch):
    monkeypatch.delenv("SEARCH_TRANSPORT", raising=False)
    monkeypatch.delenv("SEARCH_TRANSPORT_DIRECT_CONNECT_TIMEOUT_MS", raising=False)
    monkeypatch.delenv("SEARCH_TRANSPORT_DIRECT_READ_TIMEOUT_MS", raising=False)

    transport = get_search_transport()

    assert transport.connect_timeout_ms == 120000
    assert transport.read_timeout_ms == 30000


def test_module_never_references_tor_enabled():
    # Local, redundant proof of the repo-wide invariant enforced by
    # tests/test_tor_production_dark_launch.py::
    # test_tor_enabled_setters_are_a_closed_allowlist -- this module must
    # never gate proxy mode on TOR_ENABLED (that flag is scoped to Tor
    # infra/monitoring only, never to collection traffic).
    source = Path(REPO_ROOT / "scripts" / "search_transport" / "transport.py").read_text()
    assert "get_tor_enabled(" not in source
    assert "from app.config import" in source and "get_tor_enabled" not in source.split(
        "from app.config import", 1
    )[1].split(")", 1)[0]


def test_proxy_mode_activates_from_search_transport_alone_even_without_tor_enabled(monkeypatch):
    # TOR_ENABLED is intentionally NOT a gate here -- see module docstring.
    server, host, port = _listen_on_free_port()

    try:
        monkeypatch.setenv("SEARCH_TRANSPORT", "proxy")
        monkeypatch.delenv("TOR_ENABLED", raising=False)
        monkeypatch.setenv("TOR_SOCKS_HOST", host)
        monkeypatch.setenv("TOR_SOCKS_PORT", str(port))

        transport = get_search_transport()

        assert transport.mode == MODE_PROXY
        assert transport.playwright_proxy_config == {"server": f"socks5://{host}:{port}"}
    finally:
        with closing(server):
            pass


def test_tor_enabled_true_alone_never_activates_proxy_mode(monkeypatch):
    # TOR_ENABLED=true alone must never be enough to route traffic through
    # proxy -- SEARCH_TRANSPORT is the sole, explicit rollout gate.
    monkeypatch.setenv("SEARCH_TRANSPORT", "direct")
    monkeypatch.setenv("TOR_ENABLED", "true")

    transport = get_search_transport()

    assert transport.mode == MODE_DIRECT
    assert transport.playwright_proxy_config is None


def test_proxy_mode_raises_when_socks_port_unreachable(monkeypatch):
    # A closed local port that nothing is listening on.
    unused_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    unused_socket.bind(("127.0.0.1", 0))
    _, unreachable_port = unused_socket.getsockname()
    unused_socket.close()

    monkeypatch.setenv("SEARCH_TRANSPORT", "proxy")
    monkeypatch.setenv("TOR_SOCKS_HOST", "127.0.0.1")
    monkeypatch.setenv("TOR_SOCKS_PORT", str(unreachable_port))
    monkeypatch.setenv("SEARCH_TRANSPORT_PROXY_PROBE_TIMEOUT_MS", "500")

    with pytest.raises(ProxyTransportUnavailableError):
        get_search_transport()


def test_unreachable_proxy_never_falls_back_to_direct(monkeypatch):
    unused_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    unused_socket.bind(("127.0.0.1", 0))
    _, unreachable_port = unused_socket.getsockname()
    unused_socket.close()

    monkeypatch.setenv("SEARCH_TRANSPORT", "proxy")
    monkeypatch.setenv("TOR_SOCKS_HOST", "127.0.0.1")
    monkeypatch.setenv("TOR_SOCKS_PORT", str(unreachable_port))
    monkeypatch.setenv("SEARCH_TRANSPORT_PROXY_PROBE_TIMEOUT_MS", "500")

    try:
        get_search_transport()
        raised = False
    except ProxyTransportUnavailableError:
        raised = True

    # The ONLY acceptable outcome is a raised exception -- there must be
    # no code path in get_search_transport() that swallows this and
    # returns a direct-mode SearchTransport instead.
    assert raised is True


def test_invalid_mode_raises_value_error(monkeypatch):
    with pytest.raises(ValueError):
        get_search_transport(mode="carrier_pigeon")
