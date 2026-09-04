"""Tests for scripts/linkedin_auth_preflight.py (Phase 3.4K Stabilization,
Section 6) -- proves preflight transport parity with real collection:

  - preflight resolves its proxy/timeout config from EXACTLY
    scripts.search_transport.transport.get_search_transport(), the same
    function scripts/providers/linkedin_browser_provider.py calls -- never
    the legacy scripts/tor/tor_client.py path
  - proxy mode's playwright proxy dict/timeouts are wired straight through
    to browser.launch()/page timeouts unchanged
  - direct mode passes proxy=None (no proxy argument at all)
  - a configured-but-unreachable proxy fails the preflight closed (raises
    SystemExit) rather than silently falling back to direct traffic
  - the module contains no ControlPort logic and never requests a circuit
    change

No real Playwright browser, network, or LinkedIn session is used: a fake
sync_playwright stands in and records what it was called with.
"""
import socket
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import scripts.linkedin_auth_preflight as preflight_module
from scripts.search_transport.transport import get_search_transport


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


class FakePage:
    def __init__(self):
        self.default_timeout_ms = None
        self.default_navigation_timeout_ms = None
        self.goto_calls = []
        self.url = "https://www.linkedin.com/feed/"

    def set_default_timeout(self, ms):
        self.default_timeout_ms = ms

    def set_default_navigation_timeout(self, ms):
        self.default_navigation_timeout_ms = ms

    def goto(self, url, wait_until=None):
        self.goto_calls.append({"url": url, "wait_until": wait_until})

    def wait_for_timeout(self, ms):
        pass

    def title(self):
        return "LinkedIn"

    def locator(self, selector):
        raise AssertionError("preflight test should not need real DOM inspection")


class FakeContext:
    def __init__(self, page):
        self._page = page
        self.closed = False

    def new_page(self):
        return self._page

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, page):
        self.launch_kwargs = None
        self._page = page
        self.closed = False

    def new_context(self, **kwargs):
        return FakeContext(self._page)

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, browser):
        self._browser = browser
        self.launch_kwargs = None

    def launch(self, **kwargs):
        self.launch_kwargs = kwargs
        return self._browser


class FakePlaywright:
    def __init__(self, chromium):
        self.chromium = chromium

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _install_fake_playwright(monkeypatch, page=None):
    page = page or FakePage()
    browser = FakeBrowser(page)
    chromium = FakeChromium(browser)
    fake_playwright = FakePlaywright(chromium)

    monkeypatch.setattr(preflight_module, "sync_playwright", lambda: fake_playwright)
    monkeypatch.setattr(preflight_module, "assert_linkedin_authenticated", lambda page, stage=None: None)

    return chromium, browser, page


def _write_storage_state(tmp_path):
    state_path = tmp_path / "linkedin_storage_state.json"
    state_path.write_text("{}")
    return state_path


def test_direct_mode_passes_no_proxy_argument(monkeypatch, tmp_path):
    monkeypatch.delenv("SEARCH_TRANSPORT", raising=False)
    monkeypatch.setenv("LINKEDIN_AUTH_PREFLIGHT", "true")
    monkeypatch.setenv("LINKEDIN_STORAGE_STATE", str(_write_storage_state(tmp_path)))

    chromium, browser, page = _install_fake_playwright(monkeypatch)

    preflight_module.preflight_linkedin_auth()

    expected = get_search_transport()
    assert chromium.launch_kwargs["proxy"] is None
    assert page.default_navigation_timeout_ms == expected.connect_timeout_ms
    assert page.default_timeout_ms == expected.read_timeout_ms


def test_proxy_mode_matches_get_search_transport_exactly(monkeypatch, tmp_path):
    server, host, port = _listen_on_free_port()

    try:
        monkeypatch.setenv("SEARCH_TRANSPORT", "proxy")
        monkeypatch.setenv("TOR_SOCKS_HOST", host)
        monkeypatch.setenv("TOR_SOCKS_PORT", str(port))
        monkeypatch.setenv("SEARCH_TRANSPORT_PROXY_PROBE_TIMEOUT_MS", "1000")
        monkeypatch.setenv("LINKEDIN_AUTH_PREFLIGHT", "true")
        monkeypatch.setenv("LINKEDIN_STORAGE_STATE", str(_write_storage_state(tmp_path)))

        chromium, browser, page = _install_fake_playwright(monkeypatch)

        preflight_module.preflight_linkedin_auth()

        expected = get_search_transport()
        assert expected.mode == "proxy"
        assert chromium.launch_kwargs["proxy"] == expected.playwright_proxy_config
        assert page.default_navigation_timeout_ms == expected.connect_timeout_ms
        assert page.default_timeout_ms == expected.read_timeout_ms
    finally:
        server.close()


def test_unreachable_proxy_fails_preflight_closed_not_direct(monkeypatch, tmp_path):
    unreachable_port = 65534

    monkeypatch.setenv("SEARCH_TRANSPORT", "proxy")
    monkeypatch.setenv("TOR_SOCKS_HOST", "127.0.0.1")
    monkeypatch.setenv("TOR_SOCKS_PORT", str(unreachable_port))
    monkeypatch.setenv("SEARCH_TRANSPORT_PROXY_PROBE_TIMEOUT_MS", "300")
    monkeypatch.setenv("LINKEDIN_AUTH_PREFLIGHT", "true")
    monkeypatch.setenv("LINKEDIN_STORAGE_STATE", str(_write_storage_state(tmp_path)))

    chromium, browser, page = _install_fake_playwright(monkeypatch)

    with pytest.raises(SystemExit) as exc_info:
        preflight_module.preflight_linkedin_auth()

    assert "LINKEDIN_AUTH_PREFLIGHT_FAILED" in str(exc_info.value)
    # Never fell through to a direct-mode launch call as a fallback.
    assert chromium.launch_kwargs is None


def _source_excluding_module_docstring() -> str:
    # The module docstring legitimately explains, in prose, what this
    # module deliberately does NOT do (no legacy tor_client, no
    # ControlPort logic, no circuit rotation) -- which means those exact
    # words appear there on purpose. These checks care about actual CODE,
    # so the leading triple-quoted docstring block is stripped first.
    source = Path(REPO_ROOT / "scripts" / "linkedin_auth_preflight.py").read_text()
    first = source.index('"""')
    second = source.index('"""', first + 3)
    return source[:first] + source[second + 3:]


def test_module_uses_shared_transport_not_legacy_tor_client():
    code = _source_excluding_module_docstring()
    assert "tor_client" not in code
    assert "get_search_transport" in code
    assert preflight_module.get_search_transport is get_search_transport


def test_module_has_no_control_port_or_rotation_logic():
    code = _source_excluding_module_docstring()
    assert "ControlPort" not in code
    assert "control_port" not in code.lower()
    assert "rotate_circuit" not in code
    assert "maybe_rotate_circuit" not in code
