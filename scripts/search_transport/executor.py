"""Request executor (Phase 3.4K, Section 2; decoupled from Tor rotation
in the Phase 3.4K Final Pre-Commit Safety Pass).

Sits between collector business logic and the transport: it is the ONLY
place that (a) actually calls page.goto(), (b) classifies the outcome,
and (c) records the transport metric. A collector calls
RequestExecutor.navigate(page, url) and reacts to the returned
RequestResult's `.category` -- it never sees a raw status code or a raw
Playwright exception at this layer.

RequestExecutor executes, classifies, and meters -- nothing else. It
deliberately does NOT decide anything about Tor (or any other proxy's)
circuit/session state: no rotate_circuit()/maybe_rotate_circuit() call,
no ControlPort knowledge, no circuit_key, no import of anything under
scripts/tor/. A LinkedIn response classification -- including
RATE_LIMIT -- must never automatically trigger a proxy circuit change;
this module reports the classification to the caller like any other, and
the caller's existing bounded retry/backoff policy (see
scripts/search_transport/retry_policy.py and
scripts/process_search_demand_queue.py's fail_count budget) is what
decides whether/when to try again. This keeps the transport abstraction
genuinely generic: `self.transport.playwright_proxy_config` is just "some
SOCKS5 proxy dict, or None" here -- this module has no idea, and no need
to know, whether that proxy happens to be Tor. Any future Tor
circuit-rotation POLICY belongs entirely inside
scripts/tor/circuit_manager.py's own explicit, operator/test-invoked
surface (rotate_circuit(), verify_circuit(), etc.) -- never wired
automatically into this collection execution path.

Connect vs. read timeout, and what that means for Playwright specifically:
Playwright's sync API does not expose a separate socket-level connect
timeout the way a raw HTTP client (e.g. requests/httpx) would -- there is
one navigation timeout for page.goto() (covers DNS + TCP/TLS + SOCKS
handshake + first byte) and one default *action* timeout that governs
everything else (selector waits, clicks). This module maps the spec's
"connect timeout" onto page.goto()'s navigation timeout (SearchTransport.
connect_timeout_ms) and "read timeout" onto Playwright's default action
timeout (SearchTransport.read_timeout_ms, applied via
page.set_default_timeout() by the caller before navigating) -- it does
NOT claim a literal socket-level split Playwright cannot provide. This is
stated explicitly here rather than left implicit, since pretending
Playwright has a feature it doesn't would be worse than not having the
split at all.
"""
import time

from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError

from scripts.search_transport.classifier import classify_exception, classify_response, RequestResult
from scripts.search_transport.metrics import record_metric
from scripts.search_transport.transport import SearchTransport


class RequestExecutor:
    """Executes one navigation, classifies the outcome, and records a
    transport metric. Holds no circuit/rotation state of any kind -- see
    module docstring."""

    def __init__(self, transport: SearchTransport):
        self.transport = transport

    def navigate(self, page, url: str, *, auth_check_fn=None) -> RequestResult:
        """auth_check_fn, if given, is called with the Playwright `page`
        AFTER a response is received (never on an exception path, since
        there is no page content to inspect if navigation never
        completed) and must return a bool: whether the response looks like
        a login wall/checkpoint/captcha rather than real content. Kept as
        an injected callable so this module stays LinkedIn-agnostic."""
        started_at = time.monotonic()

        try:
            response = page.goto(
                url,
                wait_until="commit",
                timeout=self.transport.connect_timeout_ms,
            )

        except PlaywrightTimeoutError as error:
            result = classify_exception(error)

        except PlaywrightError as error:
            result = classify_exception(error)

        else:
            status_code = response.status if response is not None else 0
            auth_challenge_detected = bool(auth_check_fn(page)) if auth_check_fn else False
            result = classify_response(status_code, auth_challenge_detected=auth_challenge_detected)

        latency_ms = round((time.monotonic() - started_at) * 1000)

        record_metric(
            "search_transport_request",
            mode=self.transport.mode,
            category=result.category,
            status_code=result.status_code if result.status_code is not None else 0,
            latency_ms=latency_ms,
            reason=result.reason_code,
        )

        return result
