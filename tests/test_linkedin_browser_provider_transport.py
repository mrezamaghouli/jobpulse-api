"""Tests for the search-transport wiring inside
scripts/providers/linkedin_browser_provider.py (Phase 3.4K) -- proves
open_search_page()'s classification-driven control flow without any real
browser, network, or LinkedIn session:
  - a successful navigation behaves exactly as before (wait 5s, return)
  - a DOM-heuristic-only auth challenge on a 2xx response does NOT abort
    (must not become a new failure mode on the default direct path)
  - a literal 403/429 status DOES abort
  - a timeout retries once, then continues if the retry lands on
    linkedin.com, matching the pre-Phase-3.4K retry behavior
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.providers.linkedin_browser_provider import LinkedInBrowserProvider
from scripts.search_transport.classifier import RequestResult, CATEGORY_AUTH_CHALLENGE, CATEGORY_NETWORK_FAILURE, CATEGORY_SUCCESS


class FakeExecutor:
    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    def navigate(self, page, url, auth_check_fn=None):
        result = self._results[min(self.calls, len(self._results) - 1)]
        self.calls += 1
        return result


class FakePage:
    def __init__(self, url="https://www.linkedin.com/jobs/search/"):
        self.url = url
        self.waited_ms = []

    def set_default_timeout(self, ms):
        pass

    def set_default_navigation_timeout(self, ms):
        pass

    def wait_for_timeout(self, ms):
        self.waited_ms.append(ms)


def _provider_with(transport_mode="direct", results=None):
    provider = LinkedInBrowserProvider.__new__(LinkedInBrowserProvider)
    provider.transport = type("T", (), {"read_timeout_ms": 30000, "connect_timeout_ms": 120000, "mode": transport_mode})()
    provider.executor = FakeExecutor(results or [RequestResult(CATEGORY_SUCCESS, 200, "ok")])
    return provider


def test_successful_navigation_returns_normally():
    provider = _provider_with(results=[RequestResult(CATEGORY_SUCCESS, 200, "ok")])
    page = FakePage()

    provider.open_search_page(page, "https://www.linkedin.com/jobs/search/?keywords=x")

    assert page.waited_ms == [5000]


def test_dom_only_auth_challenge_on_2xx_does_not_abort():
    # This is the guard against a new false-positive failure mode: a
    # heuristic detected right after wait_until="commit" must not raise.
    provider = _provider_with(
        results=[RequestResult(CATEGORY_AUTH_CHALLENGE, 200, "auth_challenge_detected")]
    )
    page = FakePage()

    provider.open_search_page(page, "https://www.linkedin.com/jobs/search/?keywords=x")

    assert page.waited_ms == [5000]


def test_literal_403_aborts():
    provider = _provider_with(
        results=[RequestResult(CATEGORY_AUTH_CHALLENGE, 403, "http_403")]
    )
    page = FakePage()

    with pytest.raises(RuntimeError):
        provider.open_search_page(page, "https://www.linkedin.com/jobs/search/?keywords=x")


def test_literal_429_aborts():
    provider = _provider_with(
        results=[RequestResult(CATEGORY_AUTH_CHALLENGE, 429, "http_429")]
    )
    page = FakePage()

    with pytest.raises(RuntimeError):
        provider.open_search_page(page, "https://www.linkedin.com/jobs/search/?keywords=x")


def test_timeout_then_already_on_linkedin_recovers_without_raising():
    provider = _provider_with(
        results=[RequestResult(CATEGORY_NETWORK_FAILURE, None, "timeout")]
    )
    page = FakePage(url="https://www.linkedin.com/jobs/search/?keywords=x")

    provider.open_search_page(page, "https://www.linkedin.com/jobs/search/?keywords=x")

    assert page.waited_ms == [5000]
    assert provider.executor.calls == 1


def test_timeout_then_retry_succeeds():
    provider = _provider_with(
        results=[
            RequestResult(CATEGORY_NETWORK_FAILURE, None, "timeout"),
            RequestResult(CATEGORY_SUCCESS, 200, "ok"),
        ]
    )
    page = FakePage(url="about:blank")

    provider.open_search_page(page, "https://www.linkedin.com/jobs/search/?keywords=x")

    assert provider.executor.calls == 2
    assert page.waited_ms == [5000]


def test_timeout_then_retry_also_fails_raises():
    provider = _provider_with(
        results=[
            RequestResult(CATEGORY_NETWORK_FAILURE, None, "timeout"),
            RequestResult(CATEGORY_NETWORK_FAILURE, None, "dns_failure"),
        ]
    )
    page = FakePage(url="about:blank")

    with pytest.raises(RuntimeError):
        provider.open_search_page(page, "https://www.linkedin.com/jobs/search/?keywords=x")


def test_generic_network_failure_aborts_without_retry():
    provider = _provider_with(
        results=[RequestResult(CATEGORY_NETWORK_FAILURE, None, "dns_failure")]
    )
    page = FakePage()

    with pytest.raises(RuntimeError):
        provider.open_search_page(page, "https://www.linkedin.com/jobs/search/?keywords=x")

    assert provider.executor.calls == 1
