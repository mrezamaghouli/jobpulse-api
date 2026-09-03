"""Structural + behavioral regression guard (Phase 3.4K Final Pre-Commit
Safety Pass) -- proves the LinkedIn collection runtime cannot
automatically rotate a shared Tor instance:

  Queue -> linkedin_plan_collect -> collector_postgres ->
  LinkedInBrowserProvider -> SearchTransport -> RequestExecutor ->
  Playwright

  RequestExecutor -> classify -> metrics -> return RequestResult
  RATE_LIMIT -> bounded failure/retry policy (NOT Tor rotation)

scripts/search_transport/tor_manager.py (the auto-rotation POLICY layer
that used to translate a RATE_LIMIT classification directly into a
rotate_circuit() call) has been removed entirely -- there is no module
left in the collection runtime path that could make that call. The Tor
control plane (scripts/tor/circuit_manager.py) remains, but only for its
separate, explicit, operator/test-invoked manual surface (rotate_circuit()
CLI, verify_circuit(), etc. -- see tests/test_tor_circuit_manager.py,
kept intact and unmodified by this pass) -- it is never imported by any
collection runtime module.

No real Tor daemon, no real LinkedIn traffic, no real database, no real
browser: source-level static checks plus one behavioral proof using a
fake Playwright page.
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import scripts.search_transport.executor as executor_module
from scripts.search_transport.classifier import (
    CATEGORY_AUTH_CHALLENGE,
    CATEGORY_NETWORK_FAILURE,
    CATEGORY_RATE_LIMIT,
    CATEGORY_SUCCESS,
    RequestResult,
)
from scripts.search_transport.executor import RequestExecutor
from scripts.search_transport.transport import SearchTransport


# The collection runtime path this pass audits end to end. Deliberately
# does NOT include scripts/tor/circuit_manager.py or
# scripts/tor/tor_client.py -- those are the separate, legitimate control
# plane / diagnostic surface this test does not touch.
COLLECTION_RUNTIME_FILES = [
    "scripts/search_transport/executor.py",
    "scripts/search_transport/classifier.py",
    "scripts/search_transport/transport.py",
    "scripts/search_transport/retry_policy.py",
    "scripts/search_transport/metrics.py",
    "scripts/search_transport/__init__.py",
    "scripts/providers/linkedin_browser_provider.py",
    "scripts/linkedin_plan_collect.py",
    "scripts/process_search_demand_queue.py",
    "scripts/collector_postgres.py",
    "scripts/linkedin_auth_preflight.py",
]

FORBIDDEN_TOKENS = (
    "rotate_circuit(",
    "maybe_rotate_circuit(",
    "request_new_identity(",
    "Signal.NEWNYM",
    "from scripts.tor.circuit_manager",
    "import scripts.tor.circuit_manager",
    "from scripts.search_transport.tor_manager",
    "import scripts.search_transport.tor_manager",
)


def _code_excluding_module_docstring(path: Path) -> str:
    """Strips only the leading module docstring (if present) before
    substring checks -- several of these files legitimately explain, in
    prose, that they deliberately do NOT call these functions, which
    means the literal text would otherwise false-positive a blanket
    whole-file search."""
    source = path.read_text()
    stripped = source.lstrip()

    if not (stripped.startswith('"""') or stripped.startswith("'''")):
        return source

    quote = stripped[:3]
    first = source.index(quote)
    second = source.index(quote, first + 3)
    return source[:first] + source[second + 3:]


def test_search_transport_tor_manager_module_no_longer_exists():
    # The auto-rotation POLICY layer is gone entirely -- Option A from
    # the stabilization spec (no remaining legitimate role once decoupled
    # from RequestExecutor).
    assert not (REPO_ROOT / "scripts" / "search_transport" / "tor_manager.py").exists()


@pytest.mark.parametrize("relative_path", COLLECTION_RUNTIME_FILES)
def test_collection_runtime_file_has_no_rotation_call_or_import(relative_path):
    path = REPO_ROOT / relative_path
    assert path.exists(), f"expected collection runtime file missing: {relative_path}"

    code = _code_excluding_module_docstring(path)

    for token in FORBIDDEN_TOKENS:
        assert token not in code, f"{relative_path} must never contain {token!r}"


def test_request_executor_has_no_circuit_key_parameter():
    import inspect

    signature = inspect.signature(RequestExecutor.__init__)
    assert "circuit_key" not in signature.parameters


def test_request_executor_module_exposes_no_rotation_function():
    assert not hasattr(executor_module, "maybe_rotate_circuit")
    assert not hasattr(executor_module, "rotate_circuit")


class FakeResponse:
    def __init__(self, status):
        self.status = status


class FakePage:
    def __init__(self, goto_result=None, goto_exception=None):
        self._goto_result = goto_result
        self._goto_exception = goto_exception

    def goto(self, url, wait_until=None, timeout=None):
        if self._goto_exception is not None:
            raise self._goto_exception
        return self._goto_result


def _proxy_transport():
    return SearchTransport(
        mode="proxy", playwright_proxy_config={"server": "socks5://127.0.0.1:9050"},
        connect_timeout_ms=1000, read_timeout_ms=1000,
    )


@pytest.mark.parametrize(
    "goto_result,expected_category",
    [
        (FakeResponse(429), CATEGORY_RATE_LIMIT),
        (FakeResponse(403), CATEGORY_AUTH_CHALLENGE),
        (FakeResponse(200), CATEGORY_SUCCESS),
    ],
)
def test_navigate_classifies_normally_without_any_rotation_hook_present(goto_result, expected_category):
    """Behavioral proof, not just a static check: even a RATE_LIMIT
    result classified through the real RequestExecutor.navigate() code
    path, in proxy mode, completes normally and returns the expected
    RequestResult -- there is nothing left in this call path that could
    invoke a Tor rotation, mocked or otherwise."""
    page = FakePage(goto_result=goto_result)
    executor = RequestExecutor(_proxy_transport())

    result = executor.navigate(page, "https://example.com")

    assert isinstance(result, RequestResult)
    assert result.category == expected_category
