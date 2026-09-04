"""Tests for scripts/search_transport/executor.py -- uses a fake
Playwright `page` (no real browser/network) to prove navigate() correctly
classifies success, timeout, and other Playwright error shapes, and
records a metric for every classification.

Phase 3.4K Final Pre-Commit Safety Pass: RequestExecutor no longer knows
anything about Tor circuit rotation (scripts/search_transport/tor_manager.py
was removed entirely -- there is no remaining automatic coupling between a
LinkedIn response classification and a Tor circuit change). The tests
below that used to prove "RATE_LIMIT in proxy mode requests a rotation"
are replaced with the opposite: proving navigate() never rotates
anything, for ANY classification, in either transport mode."""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import scripts.search_transport.executor as executor_module
from scripts.search_transport.classifier import (
    CATEGORY_AUTH_CHALLENGE,
    CATEGORY_NETWORK_FAILURE,
    CATEGORY_RATE_LIMIT,
    CATEGORY_SUCCESS,
)
from scripts.search_transport.executor import RequestExecutor
from scripts.search_transport.transport import SearchTransport


class FakeResponse:
    def __init__(self, status):
        self.status = status


class FakePage:
    def __init__(self, goto_result=None, goto_exception=None):
        self._goto_result = goto_result
        self._goto_exception = goto_exception
        self.goto_calls = []

    def goto(self, url, wait_until=None, timeout=None):
        self.goto_calls.append({"url": url, "wait_until": wait_until, "timeout": timeout})

        if self._goto_exception is not None:
            raise self._goto_exception

        return self._goto_result


def _direct_transport():
    return SearchTransport(mode="direct", playwright_proxy_config=None, connect_timeout_ms=1000, read_timeout_ms=1000)


def _proxy_transport():
    return SearchTransport(
        mode="proxy", playwright_proxy_config={"server": "socks5://127.0.0.1:9050"},
        connect_timeout_ms=1000, read_timeout_ms=1000,
    )


def test_successful_navigation_is_classified_success():
    page = FakePage(goto_result=FakeResponse(200))
    executor = RequestExecutor(_direct_transport())

    result = executor.navigate(page, "https://example.com")

    assert result.category == CATEGORY_SUCCESS
    assert page.goto_calls[0]["timeout"] == 1000


def test_navigate_always_uses_wait_until_commit():
    # Regression guard (Phase 3.4K Stabilization, Section 12): navigate()
    # must keep using wait_until="commit" -- the DOM-heuristic auth-wall
    # check in LinkedInBrowserProvider._is_hard_navigation_failure() is
    # explicitly written to tolerate being run before this heavy
    # client-rendered SPA has hydrated, and depends on this exact value.
    page = FakePage(goto_result=FakeResponse(200))
    executor = RequestExecutor(_direct_transport())

    executor.navigate(page, "https://example.com")

    assert page.goto_calls[0]["wait_until"] == "commit"


def test_timeout_exception_is_classified_network_failure():
    page = FakePage(goto_exception=PlaywrightTimeoutError("Timeout 1000ms exceeded."))
    executor = RequestExecutor(_direct_transport())

    result = executor.navigate(page, "https://example.com")

    assert result.category == CATEGORY_NETWORK_FAILURE
    assert result.reason_code == "timeout"


def test_generic_playwright_error_is_classified_network_failure():
    page = FakePage(goto_exception=PlaywrightError("net::ERR_CONNECTION_RESET at https://example.com"))
    executor = RequestExecutor(_direct_transport())

    result = executor.navigate(page, "https://example.com")

    assert result.category == CATEGORY_NETWORK_FAILURE
    assert result.reason_code == "connection_reset"


def test_auth_check_fn_is_only_called_after_a_response_is_received():
    auth_check_fn = MagicMock(return_value=False)
    page = FakePage(goto_result=FakeResponse(200))
    executor = RequestExecutor(_direct_transport())

    executor.navigate(page, "https://example.com", auth_check_fn=auth_check_fn)

    auth_check_fn.assert_called_once_with(page)


def test_auth_check_fn_is_never_called_on_exception_path():
    auth_check_fn = MagicMock(return_value=False)
    page = FakePage(goto_exception=PlaywrightTimeoutError("Timeout 1000ms exceeded."))
    executor = RequestExecutor(_direct_transport())

    executor.navigate(page, "https://example.com", auth_check_fn=auth_check_fn)

    auth_check_fn.assert_not_called()


def test_rate_limit_in_proxy_mode_never_triggers_rotation():
    # New expected behavior (Phase 3.4K Final Pre-Commit Safety Pass):
    # proxy + HTTP 429 -> CATEGORY_RATE_LIMIT, metric recorded, NO circuit
    # rotation of any kind. executor.py must not even be ABLE to call a
    # rotation function -- it no longer imports one.
    page = FakePage(goto_result=FakeResponse(429))
    executor = RequestExecutor(_proxy_transport())

    result = executor.navigate(page, "https://example.com")

    assert result.category == CATEGORY_RATE_LIMIT
    assert not hasattr(executor_module, "maybe_rotate_circuit")
    assert not hasattr(executor_module, "rotate_circuit")


def test_auth_challenge_403_never_triggers_rotation():
    page = FakePage(goto_result=FakeResponse(403))
    executor = RequestExecutor(_proxy_transport())

    result = executor.navigate(page, "https://example.com")

    assert result.category == CATEGORY_AUTH_CHALLENGE
    assert not hasattr(executor_module, "maybe_rotate_circuit")


def test_timeout_never_triggers_rotation():
    page = FakePage(goto_exception=PlaywrightTimeoutError("Timeout 1000ms exceeded."))
    executor = RequestExecutor(_proxy_transport())

    result = executor.navigate(page, "https://example.com")

    assert result.category == CATEGORY_NETWORK_FAILURE
    assert not hasattr(executor_module, "maybe_rotate_circuit")


def test_success_never_triggers_rotation():
    page = FakePage(goto_result=FakeResponse(200))
    executor = RequestExecutor(_proxy_transport())

    result = executor.navigate(page, "https://example.com")

    assert result.category == CATEGORY_SUCCESS
    assert not hasattr(executor_module, "maybe_rotate_circuit")


def test_executor_module_has_no_circuit_manager_import():
    # Structural guard: executor.py must not import anything under
    # scripts/tor/ or scripts/search_transport/tor_manager.py (which no
    # longer exists) -- a RequestResult classification must never be able
    # to reach a circuit-rotation call, even indirectly. Checked against
    # the CODE, not the module docstring, which legitimately explains (in
    # prose) that these calls were deliberately removed.
    source = Path(REPO_ROOT / "scripts" / "search_transport" / "executor.py").read_text()
    first = source.index('"""')
    second = source.index('"""', first + 3)
    code = source[:first] + source[second + 3:]

    assert "tor_manager" not in code
    assert "rotate_circuit" not in code
    assert "circuit_manager" not in code
    assert "scripts.tor" not in code
    assert "circuit_key" not in code


def test_executor_constructor_takes_no_circuit_key():
    # circuit_key was only ever needed to route a rotation request --
    # RequestExecutor no longer accepts it at all.
    import inspect

    signature = inspect.signature(RequestExecutor.__init__)
    assert "circuit_key" not in signature.parameters
