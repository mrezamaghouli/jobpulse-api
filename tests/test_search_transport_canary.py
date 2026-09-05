"""Tests for scripts/search_transport/canary.py (Phase 3.4L).

Entirely network-free: Playwright, SearchTransport resolution, and
RequestExecutor's navigation are all faked/monkeypatched. No real
browser, Tor daemon, or network call is ever made by this test module.

Also includes static regression guards proving the canary module stays
isolated from LinkedIn targets/collector imports (Section 18 of the
Phase 3.4L spec) and from any Tor control-plane surface (Section 19).
"""
import ast
import json
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import scripts.search_transport.canary as canary
from scripts.search_transport.transport import ProxyTransportUnavailableError, SearchTransport


NEUTRAL_URL = "https://check.torproject.org/api/ip"


# ---------------------------------------------------------------------
# Fakes -- no real Playwright/browser/network involved anywhere below.
# ---------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status):
        self.status = status


class FakeLocator:
    def __init__(self, text=None, exc=None):
        self._text = text
        self._exc = exc
        self.inner_text_calls = 0

    def inner_text(self, timeout=None):
        self.inner_text_calls += 1
        if self._exc is not None:
            raise self._exc
        return self._text


class FakePage:
    def __init__(self, goto_result=None, goto_exception=None, body_text=None, body_exception=None):
        self._goto_result = goto_result
        self._goto_exception = goto_exception
        self.goto_calls = []
        self._locator = FakeLocator(text=body_text, exc=body_exception)
        self.default_timeout = None
        self.default_navigation_timeout = None

    def set_default_timeout(self, ms):
        self.default_timeout = ms

    def set_default_navigation_timeout(self, ms):
        self.default_navigation_timeout = ms

    def goto(self, url, wait_until=None, timeout=None):
        self.goto_calls.append({"url": url, "wait_until": wait_until, "timeout": timeout})
        if self._goto_exception is not None:
            raise self._goto_exception
        return self._goto_result

    def locator(self, selector):
        return self._locator


class FakeContext:
    def __init__(self, page, close_exception=None):
        self._page = page
        self.closed = False
        self.new_page_calls = 0
        self._close_exception = close_exception

    def new_page(self):
        self.new_page_calls += 1
        return self._page

    def close(self):
        self.closed = True
        if self._close_exception is not None:
            raise self._close_exception


class FakeBrowser:
    def __init__(self, context, close_exception=None):
        self._context = context
        self.closed = False
        self._close_exception = close_exception

    def new_context(self):
        return self._context

    def close(self):
        self.closed = True
        if self._close_exception is not None:
            raise self._close_exception


class FakeChromium:
    def __init__(self, browser):
        self._browser = browser
        self.launch_calls = []

    def launch(self, headless=None, proxy=None):
        self.launch_calls.append({"headless": headless, "proxy": proxy})
        return self._browser


class FakePlaywright:
    def __init__(self, browser):
        self.chromium = FakeChromium(browser)


class FakeSyncPlaywrightCM:
    def __init__(self, playwright):
        self._playwright = playwright

    def __enter__(self):
        return self._playwright

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False


def _proxy_transport():
    return SearchTransport(
        mode="proxy",
        playwright_proxy_config={"server": "socks5://tor:9050"},
        connect_timeout_ms=30000,
        read_timeout_ms=10000,
    )


def _direct_transport():
    return SearchTransport(
        mode="direct", playwright_proxy_config=None, connect_timeout_ms=1000, read_timeout_ms=1000,
    )


def _wire_success_environment(
    monkeypatch, page, target_url=NEUTRAL_URL, transport=None,
    context_close_exception=None, browser_close_exception=None,
):
    """Patches canary's module-level collaborators so run_canary() drives
    the given FakePage through a fake Playwright chain. Returns
    (browser, context) for post-run close()/call assertions."""
    context = FakeContext(page, close_exception=context_close_exception)
    browser = FakeBrowser(context, close_exception=browser_close_exception)
    playwright = FakePlaywright(browser)

    monkeypatch.setattr(canary, "get_tor_ip_check_url", lambda: target_url)
    monkeypatch.setattr(canary, "get_search_transport", lambda: transport or _proxy_transport())
    monkeypatch.setattr(canary, "sync_playwright", lambda: FakeSyncPlaywrightCM(playwright))

    return browser, context


VALID_TOR_BODY = json.dumps({"IsTor": True, "IP": "1.2.3.4"})


# ---------------------------------------------------------------------
# 1/2. Transport mode gating
# ---------------------------------------------------------------------

def test_direct_transport_is_rejected(monkeypatch):
    monkeypatch.setattr(canary, "get_tor_ip_check_url", lambda: NEUTRAL_URL)
    monkeypatch.setattr(canary, "get_search_transport", lambda: _direct_transport())

    exit_code, result = canary.run_canary()

    assert exit_code == canary.EXIT_INVALID_CONFIG
    assert result["ok"] is False
    assert result["mode"] == "direct"
    assert result["reason_code"] == "direct_mode_rejected"
    assert result["proxy_listener_reachable"] is None


def test_proxy_mode_is_accepted_and_verified(monkeypatch):
    page = FakePage(goto_result=FakeResponse(200), body_text=VALID_TOR_BODY)
    _wire_success_environment(monkeypatch, page)

    exit_code, result = canary.run_canary()

    assert exit_code == canary.EXIT_OK
    assert result["ok"] is True
    assert result["mode"] == "proxy"
    assert result["proxy_listener_reachable"] is True


# ---------------------------------------------------------------------
# 3/4/5. Unsafe/invalid target rejected BEFORE any navigation attempt
# ---------------------------------------------------------------------

def _patch_all_network_transport_stages(monkeypatch):
    """Patches all three stages that a target-URL rejection must occur
    strictly BEFORE: transport resolution, Playwright startup, and the
    request executor. Returns the three MagicMocks so callers can assert
    none of them were ever touched."""
    transport_getter = MagicMock(name="get_search_transport")
    playwright_starter = MagicMock(name="sync_playwright")
    executor_cls = MagicMock(name="RequestExecutor")

    monkeypatch.setattr(canary, "get_search_transport", transport_getter)
    monkeypatch.setattr(canary, "sync_playwright", playwright_starter)
    monkeypatch.setattr(canary, "RequestExecutor", executor_cls)

    return transport_getter, playwright_starter, executor_cls


@pytest.mark.parametrize("bad_url", [
    "https://linkedin.com/jobs",
    "https://www.linkedin.com/jobs",
    "https://jobs.linkedin.com/anything",
    "https://sub.jobs.linkedin.com/x",
])
def test_linkedin_targets_rejected_before_navigation(monkeypatch, bad_url):
    monkeypatch.setattr(canary, "get_tor_ip_check_url", lambda: bad_url)
    transport_getter, playwright_starter, executor_cls = _patch_all_network_transport_stages(monkeypatch)

    exit_code, result = canary.run_canary()

    assert exit_code == canary.EXIT_INVALID_CONFIG
    assert result["reason_code"] == "unsafe_target_url"
    transport_getter.assert_not_called()
    playwright_starter.assert_not_called()
    executor_cls.assert_not_called()
    executor_cls.return_value.navigate.assert_not_called()


@pytest.mark.parametrize("bad_url", [
    "not-a-url",
    "ftp://example.com/x",
    "",
    "https://",
])
def test_invalid_urls_rejected_before_navigation(monkeypatch, bad_url):
    monkeypatch.setattr(canary, "get_tor_ip_check_url", lambda: bad_url)
    transport_getter, playwright_starter, executor_cls = _patch_all_network_transport_stages(monkeypatch)

    exit_code, result = canary.run_canary()

    assert exit_code == canary.EXIT_INVALID_CONFIG
    assert result["failure_category"] == "INVALID_CONFIGURATION"
    transport_getter.assert_not_called()
    playwright_starter.assert_not_called()
    executor_cls.assert_not_called()
    executor_cls.return_value.navigate.assert_not_called()


# ---------------------------------------------------------------------
# 6. Proxy listener unavailable
# ---------------------------------------------------------------------

def test_proxy_listener_unavailable_is_non_zero(monkeypatch):
    monkeypatch.setattr(canary, "get_tor_ip_check_url", lambda: NEUTRAL_URL)

    def _raise_unavailable():
        raise ProxyTransportUnavailableError("SOCKS proxy tor:9050 is not reachable")

    monkeypatch.setattr(canary, "get_search_transport", _raise_unavailable)

    exit_code, result = canary.run_canary()

    assert exit_code == canary.EXIT_PROXY_UNAVAILABLE
    assert exit_code != 0
    assert result["ok"] is False
    assert result["proxy_listener_reachable"] is False
    assert result["failure_category"] == "PROXY_UNAVAILABLE"


# ---------------------------------------------------------------------
# Stabilization Section 4: an explicit, invalid SEARCH_TRANSPORT value
# (e.g. a "proxxy" typo) must map to EXIT_INVALID_CONFIG, never
# EXIT_INTERNAL_ERROR. Exercises the REAL
# scripts.search_transport.transport.get_search_transport() (not
# mocked) so this proves the actual
# app.config.InvalidSearchTransportConfigError (a ValueError subclass)
# propagation path -- no network call happens because
# get_search_transport_mode() raises before any SOCKS probe.
# ---------------------------------------------------------------------

def test_invalid_search_transport_value_maps_to_invalid_config_not_internal_error(monkeypatch):
    monkeypatch.setattr(canary, "get_tor_ip_check_url", lambda: NEUTRAL_URL)
    monkeypatch.setenv("SEARCH_TRANSPORT", "proxxy")

    exit_code, result = canary.run_canary()

    assert exit_code == canary.EXIT_INVALID_CONFIG
    assert exit_code != canary.EXIT_INTERNAL_ERROR
    assert result["ok"] is False
    assert result["failure_category"] == "INVALID_CONFIGURATION"
    assert result["reason_code"] == "invalid_transport_configuration"


def test_search_transport_direct_still_maps_to_direct_mode_rejected(monkeypatch):
    monkeypatch.setattr(canary, "get_tor_ip_check_url", lambda: NEUTRAL_URL)
    monkeypatch.setenv("SEARCH_TRANSPORT", "direct")

    exit_code, result = canary.run_canary()

    assert exit_code == canary.EXIT_INVALID_CONFIG
    assert result["mode"] == "direct"
    assert result["reason_code"] == "direct_mode_rejected"


# ---------------------------------------------------------------------
# 7. Navigation/timeout failure
# ---------------------------------------------------------------------

def test_navigation_timeout_is_non_zero(monkeypatch):
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    page = FakePage(goto_exception=PlaywrightTimeoutError("Timeout 30000ms exceeded"))
    _wire_success_environment(monkeypatch, page)

    exit_code, result = canary.run_canary()

    assert exit_code == canary.EXIT_NEUTRAL_REQUEST_FAILED
    assert exit_code != 0
    assert result["ok"] is False
    assert result["neutral_request_success"] is False
    assert result["failure_category"] == "NETWORK_FAILURE"
    assert result["reason_code"] == "timeout"


def test_navigation_non_2xx_status_is_non_zero(monkeypatch):
    page = FakePage(goto_result=FakeResponse(502))
    _wire_success_environment(monkeypatch, page)

    exit_code, result = canary.run_canary()

    assert exit_code == canary.EXIT_NEUTRAL_REQUEST_FAILED
    assert result["neutral_request_success"] is False
    assert result["status_code"] == 502


# ---------------------------------------------------------------------
# 8-13. IsTor/IP semantic verification
# ---------------------------------------------------------------------

def test_http_success_istor_true_valid_ip_exits_zero(monkeypatch):
    page = FakePage(goto_result=FakeResponse(200), body_text=VALID_TOR_BODY)
    _wire_success_environment(monkeypatch, page)

    exit_code, result = canary.run_canary()

    assert exit_code == canary.EXIT_OK
    assert result["ok"] is True
    assert result["tor_route_verified"] is True
    assert result["observed_exit_ip"] == "1.2.3.4"
    assert result["status_code"] == 200


def test_http_success_istor_false_is_non_zero(monkeypatch):
    body = json.dumps({"IsTor": False, "IP": "1.2.3.4"})
    page = FakePage(goto_result=FakeResponse(200), body_text=body)
    _wire_success_environment(monkeypatch, page)

    exit_code, result = canary.run_canary()

    assert exit_code == canary.EXIT_TOR_ROUTE_VERIFICATION_FAILED
    assert exit_code != 0
    assert result["ok"] is False
    assert result["tor_route_verified"] is False
    assert result["reason_code"] == "is_tor_not_true"


def test_http_success_missing_istor_is_non_zero(monkeypatch):
    body = json.dumps({"IP": "1.2.3.4"})
    page = FakePage(goto_result=FakeResponse(200), body_text=body)
    _wire_success_environment(monkeypatch, page)

    exit_code, result = canary.run_canary()

    assert exit_code == canary.EXIT_TOR_ROUTE_VERIFICATION_FAILED
    assert result["reason_code"] == "is_tor_not_true"


def test_http_success_missing_ip_is_non_zero(monkeypatch):
    body = json.dumps({"IsTor": True})
    page = FakePage(goto_result=FakeResponse(200), body_text=body)
    _wire_success_environment(monkeypatch, page)

    exit_code, result = canary.run_canary()

    assert exit_code == canary.EXIT_TOR_ROUTE_VERIFICATION_FAILED
    assert result["reason_code"] == "missing_ip"


def test_http_success_empty_ip_is_non_zero(monkeypatch):
    body = json.dumps({"IsTor": True, "IP": ""})
    page = FakePage(goto_result=FakeResponse(200), body_text=body)
    _wire_success_environment(monkeypatch, page)

    exit_code, result = canary.run_canary()

    assert exit_code == canary.EXIT_TOR_ROUTE_VERIFICATION_FAILED
    assert result["reason_code"] == "missing_ip"


def test_invalid_json_is_non_zero(monkeypatch):
    page = FakePage(goto_result=FakeResponse(200), body_text="not json at all")
    _wire_success_environment(monkeypatch, page)

    exit_code, result = canary.run_canary()

    assert exit_code == canary.EXIT_TOR_ROUTE_VERIFICATION_FAILED
    assert result["reason_code"] == "invalid_json"


def test_non_object_json_is_non_zero(monkeypatch):
    page = FakePage(goto_result=FakeResponse(200), body_text=json.dumps([1, 2, 3]))
    _wire_success_environment(monkeypatch, page)

    exit_code, result = canary.run_canary()

    assert exit_code == canary.EXIT_TOR_ROUTE_VERIFICATION_FAILED
    assert result["reason_code"] == "non_object_json"


# ---------------------------------------------------------------------
# 14/15. Browser/context always closed
# ---------------------------------------------------------------------

def test_browser_and_context_closed_on_success(monkeypatch):
    page = FakePage(goto_result=FakeResponse(200), body_text=VALID_TOR_BODY)
    browser, context = _wire_success_environment(monkeypatch, page)

    exit_code, _ = canary.run_canary()

    assert exit_code == canary.EXIT_OK
    assert context.closed is True
    assert browser.closed is True


def test_browser_and_context_closed_on_failure(monkeypatch):
    body = json.dumps({"IsTor": False, "IP": "1.2.3.4"})
    page = FakePage(goto_result=FakeResponse(200), body_text=body)
    browser, context = _wire_success_environment(monkeypatch, page)

    exit_code, _ = canary.run_canary()

    assert exit_code != canary.EXIT_OK
    assert context.closed is True
    assert browser.closed is True


# ---------------------------------------------------------------------
# Cleanup-exception hardening (Phase 3.4L stabilization Section 2): a
# close() failure must never override an already-computed result/exit
# code, turn a navigation failure into INTERNAL_ERROR, trigger another
# navigation, or leak a traceback. Exactly one close() attempt is made
# per object regardless of whether it raises.
# ---------------------------------------------------------------------

def test_context_close_raising_does_not_override_successful_result(monkeypatch):
    page = FakePage(goto_result=FakeResponse(200), body_text=VALID_TOR_BODY)
    browser, context = _wire_success_environment(
        monkeypatch, page, context_close_exception=RuntimeError("context close boom"),
    )

    exit_code, result = canary.run_canary()

    assert exit_code == canary.EXIT_OK
    assert result["ok"] is True
    assert result["failure_category"] is None
    assert result["reason_code"] is None
    assert context.closed is True
    assert browser.closed is True
    assert len(page.goto_calls) == 1


def test_browser_close_raising_does_not_override_successful_result(monkeypatch):
    page = FakePage(goto_result=FakeResponse(200), body_text=VALID_TOR_BODY)
    browser, context = _wire_success_environment(
        monkeypatch, page, browser_close_exception=RuntimeError("browser close boom"),
    )

    exit_code, result = canary.run_canary()

    assert exit_code == canary.EXIT_OK
    assert result["ok"] is True
    assert result["failure_category"] is None
    assert result["reason_code"] is None
    assert context.closed is True
    assert browser.closed is True
    assert len(page.goto_calls) == 1


def test_context_close_raising_does_not_override_known_failure(monkeypatch):
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    page = FakePage(goto_exception=PlaywrightTimeoutError("Timeout 30000ms exceeded"))
    browser, context = _wire_success_environment(
        monkeypatch, page, context_close_exception=RuntimeError("context close boom"),
    )

    exit_code, result = canary.run_canary()

    # Must remain the ORIGINAL navigation-failure classification, not be
    # reclassified as EXIT_INTERNAL_ERROR because cleanup also failed.
    assert exit_code == canary.EXIT_NEUTRAL_REQUEST_FAILED
    assert result["failure_category"] == "NETWORK_FAILURE"
    assert result["reason_code"] == "timeout"
    assert context.closed is True
    assert browser.closed is True
    assert len(page.goto_calls) == 1


def test_browser_close_raising_does_not_override_known_failure(monkeypatch):
    body = json.dumps({"IsTor": False, "IP": "1.2.3.4"})
    page = FakePage(goto_result=FakeResponse(200), body_text=body)
    browser, context = _wire_success_environment(
        monkeypatch, page, browser_close_exception=RuntimeError("browser close boom"),
    )

    exit_code, result = canary.run_canary()

    assert exit_code == canary.EXIT_TOR_ROUTE_VERIFICATION_FAILED
    assert result["failure_category"] == "TOR_ROUTE_VERIFICATION_FAILED"
    assert result["reason_code"] == "is_tor_not_true"
    assert context.closed is True
    assert browser.closed is True
    assert len(page.goto_calls) == 1


# ---------------------------------------------------------------------
# 16/17. Exactly one navigation, no retry/fallback
# ---------------------------------------------------------------------

def test_exactly_one_navigation_occurs_on_success(monkeypatch):
    page = FakePage(goto_result=FakeResponse(200), body_text=VALID_TOR_BODY)
    _wire_success_environment(monkeypatch, page)

    canary.run_canary()

    assert len(page.goto_calls) == 1


def test_exactly_one_navigation_occurs_on_failure_no_retry(monkeypatch):
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    page = FakePage(goto_exception=PlaywrightTimeoutError("Timeout 30000ms exceeded"))
    _wire_success_environment(monkeypatch, page)

    canary.run_canary()

    assert len(page.goto_calls) == 1


# ---------------------------------------------------------------------
# 18. Output contains no raw body/cookies/headers
# ---------------------------------------------------------------------

def test_output_contains_no_raw_body_cookies_headers(monkeypatch):
    page = FakePage(goto_result=FakeResponse(200), body_text=VALID_TOR_BODY)
    _wire_success_environment(monkeypatch, page)

    _, result = canary.run_canary()
    serialized = json.dumps(result)

    assert "cookie" not in serialized.lower()
    assert "header" not in serialized.lower()
    assert VALID_TOR_BODY not in serialized
    allowed_keys = {
        "ok", "mode", "proxy_listener_reachable", "neutral_request_success",
        "tor_route_verified", "status_code", "latency_ms", "failure_category",
        "reason_code", "observed_exit_ip", "checked_at",
    }
    assert set(result.keys()) == allowed_keys


# ---------------------------------------------------------------------
# Pure parser tests (network-free by construction)
# ---------------------------------------------------------------------

def test_parse_neutral_tor_response_valid():
    ip = canary.parse_neutral_tor_response(json.dumps({"IsTor": True, "IP": "5.6.7.8"}))
    assert ip == "5.6.7.8"


def test_parse_neutral_tor_response_rejects_truthy_non_boolean_istor():
    with pytest.raises(canary.TorRouteVerificationError):
        canary.parse_neutral_tor_response(json.dumps({"IsTor": 1, "IP": "5.6.7.8"}))


def test_parse_neutral_tor_response_rejects_invalid_json():
    with pytest.raises(canary.TorRouteVerificationError):
        canary.parse_neutral_tor_response("{not json")


def test_parse_neutral_tor_response_rejects_non_object_json():
    with pytest.raises(canary.TorRouteVerificationError):
        canary.parse_neutral_tor_response("42")


def test_validate_target_url_accepts_neutral_url():
    assert canary.validate_target_url(NEUTRAL_URL) == NEUTRAL_URL


def test_validate_target_url_rejects_linkedin():
    with pytest.raises(canary.CanaryConfigError):
        canary.validate_target_url("https://www.linkedin.com/jobs")


# ---------------------------------------------------------------------
# Stabilization Section 5: IP semantic validation must use
# ipaddress.ip_address(), not "any truthy non-empty string".
# ---------------------------------------------------------------------

def test_validate_and_normalize_ip_accepts_valid_ipv4():
    assert canary._validate_and_normalize_ip("203.0.113.5") == "203.0.113.5"


def test_validate_and_normalize_ip_accepts_valid_ipv6():
    ip = canary._validate_and_normalize_ip("2001:db8::1")
    assert ip == "2001:db8::1"


def test_validate_and_normalize_ip_strips_whitespace():
    assert canary._validate_and_normalize_ip("  203.0.113.5  ") == "203.0.113.5"


def test_validate_and_normalize_ip_rejects_whitespace_only():
    with pytest.raises(canary.TorRouteVerificationError) as excinfo:
        canary._validate_and_normalize_ip("   ")
    assert excinfo.value.reason_code == "missing_ip"


def test_validate_and_normalize_ip_rejects_empty_string():
    with pytest.raises(canary.TorRouteVerificationError) as excinfo:
        canary._validate_and_normalize_ip("")
    assert excinfo.value.reason_code == "missing_ip"


def test_validate_and_normalize_ip_rejects_malformed_ipv4():
    with pytest.raises(canary.TorRouteVerificationError) as excinfo:
        canary._validate_and_normalize_ip("999.999.999.999")
    assert excinfo.value.reason_code == "invalid_ip"


def test_validate_and_normalize_ip_rejects_malformed_ipv6():
    with pytest.raises(canary.TorRouteVerificationError) as excinfo:
        canary._validate_and_normalize_ip("2001:db8::gggg")
    assert excinfo.value.reason_code == "invalid_ip"


def test_validate_and_normalize_ip_rejects_non_ip_hostname():
    with pytest.raises(canary.TorRouteVerificationError) as excinfo:
        canary._validate_and_normalize_ip("example.com")
    assert excinfo.value.reason_code == "invalid_ip"


def test_validate_and_normalize_ip_rejects_non_string():
    with pytest.raises(canary.TorRouteVerificationError) as excinfo:
        canary._validate_and_normalize_ip(12345)
    assert excinfo.value.reason_code == "missing_ip"


def test_parse_neutral_tor_response_rejects_malformed_ip_end_to_end():
    with pytest.raises(canary.TorRouteVerificationError) as excinfo:
        canary.parse_neutral_tor_response(json.dumps({"IsTor": True, "IP": "not-an-ip"}))
    assert excinfo.value.reason_code == "invalid_ip"


def test_parse_neutral_tor_response_normalizes_ipv6_end_to_end():
    ip = canary.parse_neutral_tor_response(
        json.dumps({"IsTor": True, "IP": "2001:0db8:0000:0000:0000:0000:0000:0001"})
    )
    assert ip == "2001:db8::1"


# ---------------------------------------------------------------------
# Section 18: static no-LinkedIn / no-collector-import guard
# ---------------------------------------------------------------------

CANARY_SOURCE_PATH = REPO_ROOT / "scripts" / "search_transport" / "canary.py"
CANARY_SOURCE = CANARY_SOURCE_PATH.read_text()

_FORBIDDEN_COLLECTOR_REFERENCES = (
    "linkedin_browser_provider",
    "linkedin_plan_collect",
    "process_search_demand_queue",
    "collector_postgres",
)


def test_canary_source_has_no_collector_or_provider_imports():
    for forbidden in _FORBIDDEN_COLLECTOR_REFERENCES:
        assert forbidden not in CANARY_SOURCE, (
            f"canary.py must not reference {forbidden!r}"
        )


CANARY_AST = ast.parse(CANARY_SOURCE, filename=str(CANARY_SOURCE_PATH))


def _string_constants_containing(tree, needle):
    """Yields (lineno, value) for every string literal constant in the
    AST whose value contains `needle` case-insensitively -- inspects the
    actual parsed code (not just source lines), so it also catches a
    literal built via implicit string concatenation and skips comments
    entirely (comments aren't AST nodes at all)."""
    needle = needle.lower()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if needle in node.value.lower():
                yield node.lineno, node.value


def _function_line_range(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node.lineno, node.end_lineno
    raise AssertionError(f"function {name!r} not found in canary.py")


def test_linkedin_domain_string_literals_only_appear_inside_hostname_denylist_function():
    """Stronger replacement for a line-grep check: uses the AST to find
    EVERY string literal in canary.py containing the literal domain
    fragment 'linkedin.com' (a usable request target/hostname, as
    opposed to the word "LinkedIn" appearing in ordinary prose/docstrings
    -- e.g. "no LinkedIn auth", which is expected and fine) and proves
    each one is physically inside _is_linkedin_hostname()'s own body --
    the only place a literal LinkedIn domain string is allowed to exist.
    A hard-coded LinkedIn request URL anywhere else in the file (the
    weakness of the earlier grep-based check) would fail this test."""
    start, end = _function_line_range(CANARY_AST, "_is_linkedin_hostname")

    offenders = [
        (lineno, value) for lineno, value in _string_constants_containing(CANARY_AST, "linkedin.com")
        if not (start <= lineno <= end)
    ]

    assert not offenders, (
        f"linkedin.com-referencing string literal(s) found OUTSIDE "
        f"_is_linkedin_hostname() (lines {start}-{end}): {offenders}"
    )


def test_linkedin_word_only_appears_in_documentation_or_denylist_context():
    """Complementary, broader sweep: the bare word 'linkedin' (any case)
    may appear elsewhere in docstrings/messages to EXPLAIN the isolation
    boundary (e.g. "no LinkedIn auth", "non-LinkedIn Tor-check
    endpoint") -- but every such occurrence must be prose, never a
    dotted hostname fragment other than 'linkedin.com' itself (already
    covered by the stricter test above). This guards against a
    near-miss like a hostname typo ("linkedin.co", "linked-in.com")
    being introduced as an actual target while still being source-review
    plausible."""
    suspicious_domain_pattern = re.compile(
        r"linked[\-_]?in\.(com|co|net|org|io)", re.IGNORECASE
    )
    for lineno, value in _string_constants_containing(CANARY_AST, "linkedin"):
        matches = suspicious_domain_pattern.findall(value)
        if matches:
            start, end = _function_line_range(CANARY_AST, "_is_linkedin_hostname")
            assert start <= lineno <= end, (
                f"Suspicious LinkedIn-like domain fragment at canary.py:{lineno}: {value!r}"
            )


# ---------------------------------------------------------------------
# Section 19: static no-control-plane guard
# ---------------------------------------------------------------------

_FORBIDDEN_CONTROL_PLANE_REFERENCES = (
    "scripts.tor.circuit_manager",
    "from scripts.tor",
    "import scripts.tor",
    "rotate_circuit",
    "request_new_identity",
    "Signal.NEWNYM",
    "stem.control",
    "Controller.from_port",
)

# psycopg2/stem are checked as actual import statements, not bare
# substrings -- the module docstrings legitimately SAY "no psycopg2
# import" / "no stem import" to document the isolation boundary, and a
# naive substring check would flag that documentation as a violation.
_FORBIDDEN_IMPORT_PATTERNS = (
    re.compile(r"^\s*import\s+psycopg2\b", re.MULTILINE),
    re.compile(r"^\s*from\s+psycopg2\b", re.MULTILINE),
    re.compile(r"^\s*import\s+stem\b", re.MULTILINE),
    re.compile(r"^\s*from\s+stem\b", re.MULTILINE),
)


def test_canary_source_has_no_control_plane_references():
    offenders = [name for name in _FORBIDDEN_CONTROL_PLANE_REFERENCES if name in CANARY_SOURCE]
    assert not offenders, f"canary.py must not reference: {offenders}"

    import_offenders = [p.pattern for p in _FORBIDDEN_IMPORT_PATTERNS if p.search(CANARY_SOURCE)]
    assert not import_offenders, f"canary.py must not import psycopg2/stem: {import_offenders}"


def _ast_imported_module_names(tree) -> set:
    """Collects every module name canary.py actually imports, from the
    parsed AST rather than fragile line-prefix/substring matching --
    `import X` contributes X (and each dotted parent, e.g. "scripts",
    "scripts.tor" for "scripts.tor.circuit_manager", so a bare `import
    scripts.tor` is caught too), `from X import Y` contributes X."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                names.update(".".join(parts[: i + 1]) for i in range(len(parts)))
        elif isinstance(node, ast.ImportFrom) and node.module:
            parts = node.module.split(".")
            names.update(".".join(parts[: i + 1]) for i in range(len(parts)))
    return names


def test_canary_module_does_not_import_forbidden_modules():
    """AST-based import guard (Phase 3.4L stabilization Section 7) --
    inspects actual parsed Import/ImportFrom nodes rather than a
    substring/line-prefix heuristic, so it cannot be fooled by an import
    written unusually (e.g. multi-line, aliased, or semicolon-joined) and
    cannot false-positive on a docstring merely mentioning a module name."""
    imported = _ast_imported_module_names(CANARY_AST)

    forbidden_exact = {
        "scripts.providers.linkedin_browser_provider",
        "scripts.process_search_demand_queue",
        "scripts.linkedin_plan_collect",
        "scripts.collector_postgres",
        "scripts.tor.circuit_manager",
        "scripts.tor.observability",
        "scripts.tor.verify_tor_connectivity",
        "psycopg2",
        "stem",
    }

    offenders = imported & forbidden_exact
    assert not offenders, f"canary.py imports forbidden module(s): {sorted(offenders)}"

    # Also catch any deeper forbidden submodule not explicitly listed
    # above (e.g. a hypothetical future `stem.control` or
    # `scripts.tor.something_else`) via prefix matching on the collected
    # dotted-parent set.
    forbidden_prefixes = ("stem", "psycopg2", "scripts.tor", "scripts.providers.linkedin_browser_provider")
    prefixed_offenders = {
        name for name in imported
        if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden_prefixes)
    }
    assert not prefixed_offenders, f"canary.py imports forbidden module(s): {sorted(prefixed_offenders)}"
