"""Request result classification (Phase 3.4K, Section 4).

Turns a raw HTTP-ish outcome (a status code, or an exception raised while
attempting the request) into a small, structured RequestResult -- never
lets a collector branch on a raw status code or a raw exception type
directly. This is deliberately generic and LinkedIn-agnostic: no LinkedIn
URLs, selectors, or cookie/session inspection live here (same boundary
scripts/tor/local_http_simulator.py already holds itself to). A caller
that needs LinkedIn-specific auth-wall/captcha detection (e.g.
LinkedInBrowserProvider.detect_linkedin_auth_state) computes that signal
itself and passes it in as `auth_challenge_detected` -- this module never
inspects page content.

Four categories only, matching the Phase 3.4K spec exactly:
  SUCCESS         -- 2xx/3xx, no auth challenge detected
  RATE_LIMIT       -- HTTP 429
  AUTH_CHALLENGE   -- HTTP 403, or caller-detected login wall/captcha
  NETWORK_FAILURE  -- timeout, connection reset, DNS failure, proxy
                      unavailable, or any other non-2xx/3xx/429/403 status

This module only produces the classification; it never decides what to
do with it. In particular, RATE_LIMIT is an ordinary classification like
any other here -- it must NEVER automatically trigger a Tor circuit
change (see scripts/search_transport/executor.py's module docstring).
The caller's existing bounded retry/backoff policy (see
scripts/search_transport/retry_policy.py) is what decides whether/when
to try again.
"""
from dataclasses import dataclass
from typing import Optional


CATEGORY_SUCCESS = "SUCCESS"
CATEGORY_RATE_LIMIT = "RATE_LIMIT"
CATEGORY_AUTH_CHALLENGE = "AUTH_CHALLENGE"
CATEGORY_NETWORK_FAILURE = "NETWORK_FAILURE"

_ALLOWED_CATEGORIES = {
    CATEGORY_SUCCESS,
    CATEGORY_RATE_LIMIT,
    CATEGORY_AUTH_CHALLENGE,
    CATEGORY_NETWORK_FAILURE,
}


@dataclass(frozen=True)
class RequestResult:
    """category is always one of the four constants above. status_code is
    None for exception-derived results (no response was ever received).
    reason_code is a short, normalized, machine-readable token -- never
    raw exception text -- safe to pass straight into
    scripts/search_transport/metrics.py or scripts/tor/circuit_manager.py's
    emit_event() detail (which enforces its own allowlist and would reject
    free text anyway)."""

    category: str
    status_code: Optional[int]
    reason_code: str

    def __post_init__(self):
        if self.category not in _ALLOWED_CATEGORIES:
            raise ValueError(f"Invalid RequestResult category: {self.category!r}")


def classify_response(status_code: int, auth_challenge_detected: bool = False) -> RequestResult:
    """Classifies a RECEIVED response. auth_challenge_detected is computed
    by the caller (e.g. DOM-based login-wall/captcha detection) -- this
    function never looks past the status code on its own, since a 200
    status code alone cannot distinguish a real jobs page from a login
    wall LinkedIn served with a 200."""
    if auth_challenge_detected:
        return RequestResult(CATEGORY_AUTH_CHALLENGE, status_code, "auth_challenge_detected")

    if status_code == 429:
        return RequestResult(CATEGORY_RATE_LIMIT, status_code, "http_429")

    if status_code == 403:
        return RequestResult(CATEGORY_AUTH_CHALLENGE, status_code, "http_403")

    if 200 <= status_code < 400:
        return RequestResult(CATEGORY_SUCCESS, status_code, "ok")

    return RequestResult(CATEGORY_NETWORK_FAILURE, status_code, f"http_{status_code}")


def classify_exception(exc: BaseException) -> RequestResult:
    """Classifies a FAILED request attempt (no response was received).
    Matches on the exception's type name and message using normalized,
    case-insensitive substring checks -- deliberately loose, since the
    concrete exception types differ between Playwright
    (playwright.sync_api.TimeoutError / Error, whose messages contain
    Chromium's net::ERR_* codes) and scripts/tor/local_http_simulator.py's
    SimulatedTimeout/SimulatedConnectionFailure (used by this module's own
    tests). Every branch returns NETWORK_FAILURE -- there is no
    exception-derived path to RATE_LIMIT or AUTH_CHALLENGE, since neither
    can be observed without an actual response."""
    type_name = type(exc).__name__.lower()
    message = str(exc).lower()

    if "timeout" in type_name or "timeout" in message or "err_timed_out" in message:
        return RequestResult(CATEGORY_NETWORK_FAILURE, None, "timeout")

    if "proxy" in message or "err_proxy_connection_failed" in message or "err_socks" in message:
        return RequestResult(CATEGORY_NETWORK_FAILURE, None, "proxy_unavailable")

    if (
        "name_not_resolved" in message
        or "getaddrinfo" in message
        or "dns" in message
        or "err_name_not_resolved" in message
    ):
        return RequestResult(CATEGORY_NETWORK_FAILURE, None, "dns_failure")

    if (
        "connectionfailure" in type_name
        or "connection reset" in message
        or "connection refused" in message
        or "err_connection_reset" in message
        or "err_connection_refused" in message
        or "err_connection_closed" in message
    ):
        return RequestResult(CATEGORY_NETWORK_FAILURE, None, "connection_reset")

    return RequestResult(CATEGORY_NETWORK_FAILURE, None, "unknown_network_error")
