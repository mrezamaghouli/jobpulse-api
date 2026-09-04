"""Phase 3.4L: isolated, SOCKS-only, neutral-target search-transport
canary.

This is a standalone, one-shot health check for the Phase 3.4K
SearchTransport proxy path -- deliberately NOT part of the
tor-diagnostic service (scripts/tor/production_dark_launch_check.py),
which carries PostgreSQL/ControlPort/bootstrap-persistence concerns this
canary has no reason to depend on. This module is:

  * SOCKS-only    -- resolves transport via get_search_transport() exactly
                      as production collection would, and uses only
                      transport.playwright_proxy_config (a SOCKS5 URL).
  * database-free  -- no psycopg2 import, no PostgreSQL connection.
  * ControlPort-free -- no stem import, no Controller, no NEWNYM, no
                      TOR_CONTROL_* awareness of any kind.
  * secret-free    -- no TOR_CONTROL_PASSWORD(_FILE) is read or needed.
  * collector-free -- no import of any provider/queue/collector module.
  * session-free   -- no cookies, no storage-state, no LinkedIn auth.

It proves exactly one thing: that a real Playwright Chromium navigation,
launched with the SAME transport configuration selector production
collection would use (get_search_transport(), never forced to
mode="proxy" in code -- the caller's environment does that), can reach a
neutral, non-LinkedIn Tor-check endpoint through the configured SOCKS
proxy AND that the endpoint's own response semantically confirms Tor
routing (IsTor is exactly True, plus a non-empty IP). An HTTP 200 alone
is never treated as proof of Tor routing -- see
TorRouteVerificationError and parse_neutral_tor_response() below.

Exactly one neutral request per invocation. No retry loop, no circuit
rotation, no fallback to direct transport on failure -- any unsuccessful
run must exit non-zero (see the EXIT_* constants).
"""
import ipaddress
import json
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from playwright.sync_api import (
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from app.config import get_tor_ip_check_url
from scripts.search_transport.classifier import CATEGORY_SUCCESS
from scripts.search_transport.executor import RequestExecutor
from scripts.search_transport.transport import (
    MODE_PROXY,
    ProxyTransportUnavailableError,
    get_search_transport,
)


# Stable, documented, unit-tested exit codes (Phase 3.4L spec Section 8).
# ANY unsuccessful run returns non-zero -- there is no "ran but skipped"
# exit-0 path here, unlike scripts/search_transport_benchmark.py's main().
EXIT_OK = 0
EXIT_INVALID_CONFIG = 2
EXIT_PROXY_UNAVAILABLE = 3
EXIT_NEUTRAL_REQUEST_FAILED = 4
EXIT_TOR_ROUTE_VERIFICATION_FAILED = 5
EXIT_INTERNAL_ERROR = 6


class CanaryConfigError(Exception):
    """Raised for an invalid/unsafe canary configuration (target URL
    scheme/hostname problems, a LinkedIn target, or a rejected transport
    mode) -- always caught before any network/browser call is made for a
    target-URL problem, and maps to EXIT_INVALID_CONFIG."""

    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


class TorRouteVerificationError(Exception):
    """Raised when the neutral endpoint's response body does not
    semantically confirm Tor routing (invalid JSON, non-object JSON,
    IsTor missing/false, or a missing/empty IP) -- an HTTP 200 alone
    never satisfies this. Maps to EXIT_TOR_ROUTE_VERIFICATION_FAILED."""

    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


def _is_linkedin_hostname(hostname: str) -> bool:
    normalized = hostname.lower().rstrip(".")
    return normalized == "linkedin.com" or normalized.endswith(".linkedin.com")


def validate_target_url(url: str) -> str:
    """Validates the configured neutral target URL BEFORE any
    browser/network request is made. Deliberately does not accept a CLI
    override -- the only source of the target is TOR_IP_CHECK_URL (via
    get_tor_ip_check_url()), so there is no code path that encourages an
    arbitrary or LinkedIn target."""
    if not url or not isinstance(url, str):
        raise CanaryConfigError("invalid_url", "TOR_IP_CHECK_URL is empty or not a string")

    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise CanaryConfigError(
            "invalid_url_scheme",
            f"TOR_IP_CHECK_URL scheme must be http or https, got {parsed.scheme!r}",
        )

    hostname = parsed.hostname

    if not hostname:
        raise CanaryConfigError(
            "missing_hostname", "TOR_IP_CHECK_URL has no hostname"
        )

    if _is_linkedin_hostname(hostname):
        raise CanaryConfigError(
            "unsafe_target_url",
            f"TOR_IP_CHECK_URL host {hostname!r} is a LinkedIn host -- forbidden for this canary",
        )

    return url


def _validate_and_normalize_ip(raw_ip) -> str:
    """Validates that raw_ip is a real, parseable IPv4 or IPv6 address --
    NOT merely a truthy non-empty string. "not-an-ip", "example.com", a
    malformed address, "", and whitespace-only are all rejected
    explicitly. Uses the standard library's ipaddress.ip_address() (after
    stripping surrounding whitespace) rather than any hand-rolled regex,
    and returns ip_address()'s own normalized string form."""
    if not isinstance(raw_ip, str):
        raise TorRouteVerificationError(
            "missing_ip", f"Neutral endpoint did not return a usable IP: {raw_ip!r}"
        )

    stripped = raw_ip.strip()

    if not stripped:
        raise TorRouteVerificationError(
            "missing_ip", "Neutral endpoint returned an empty/whitespace-only IP"
        )

    try:
        parsed_ip = ipaddress.ip_address(stripped)
    except ValueError:
        raise TorRouteVerificationError(
            "invalid_ip", f"Neutral endpoint did not return a valid IPv4/IPv6 address: {raw_ip!r}"
        )

    return str(parsed_ip)


def parse_neutral_tor_response(body_text: str) -> str:
    """Pure parser/validator for the neutral Tor-check endpoint's response
    body. Deliberately local to this module (not imported from
    scripts/tor/verify_tor_connectivity.py, which pulls in
    psycopg2/stem/circuit_manager) -- keeps the canary's isolation
    boundary strong. Returns the observed exit IP (normalized via
    ipaddress.ip_address()) on success; raises TorRouteVerificationError
    for every other case (invalid JSON, non-object JSON, IsTor
    missing/not exactly True, missing/empty/malformed IP -- see
    _validate_and_normalize_ip())."""
    try:
        payload = json.loads(body_text)
    except json.JSONDecodeError as error:
        raise TorRouteVerificationError(
            "invalid_json", f"Neutral endpoint did not return valid JSON: {error}"
        ) from error

    if not isinstance(payload, dict):
        raise TorRouteVerificationError(
            "non_object_json",
            f"Neutral endpoint returned non-object JSON: {type(payload).__name__}",
        )

    is_tor = payload.get("IsTor")

    if is_tor is not True:
        raise TorRouteVerificationError(
            "is_tor_not_true", f"Neutral endpoint reports IsTor={is_tor!r}"
        )

    return _validate_and_normalize_ip(payload.get("IP"))


def _empty_result() -> dict:
    return {
        "ok": False,
        "mode": None,
        "proxy_listener_reachable": None,
        "neutral_request_success": None,
        "tor_route_verified": None,
        "status_code": None,
        "latency_ms": None,
        "failure_category": None,
        "reason_code": None,
        "observed_exit_ip": None,
        "checked_at": None,
    }


def run_canary() -> "tuple[int, dict]":
    """Performs exactly one neutral canary check and returns
    (exit_code, result_dict). Thin wrapper around _run_canary_check() that
    stamps checked_at AFTER the check has actually run (whichever path it
    took -- success, a rejected/invalid config, or a failure) so the
    timestamp reflects when the result was decided, not merely when the
    empty result scaffold was created."""
    exit_code, result = _run_canary_check()
    result["checked_at"] = datetime.now(timezone.utc).isoformat()
    return exit_code, result


def _run_canary_check() -> "tuple[int, dict]":
    """Never raises -- every expected failure mode is caught and mapped to
    a stable exit code; unexpected exceptions are caught by the outermost
    handler and mapped to EXIT_INTERNAL_ERROR with a sanitized reason,
    never a raw traceback."""
    result = _empty_result()

    try:
        target_url = get_tor_ip_check_url()
        validate_target_url(target_url)
    except CanaryConfigError as error:
        result["failure_category"] = "INVALID_CONFIGURATION"
        result["reason_code"] = error.reason_code
        return EXIT_INVALID_CONFIG, result

    try:
        transport = get_search_transport()
    except ProxyTransportUnavailableError:
        result["proxy_listener_reachable"] = False
        result["failure_category"] = "PROXY_UNAVAILABLE"
        result["reason_code"] = "proxy_listener_unreachable"
        return EXIT_PROXY_UNAVAILABLE, result
    except ValueError:
        # get_search_transport() -> get_search_transport_mode() raises
        # app.config.InvalidSearchTransportConfigError (a ValueError
        # subclass) for an explicitly-set, unrecognized SEARCH_TRANSPORT
        # value (e.g. a "proxxy" typo). That is a configuration problem,
        # not an unexpected internal failure -- it must map to
        # EXIT_INVALID_CONFIG, never EXIT_INTERNAL_ERROR. Caught as the
        # bare ValueError base (rather than importing the specific
        # subclass) so this module has no import-time coupling to
        # app.config's exception hierarchy beyond the getters it already
        # uses.
        result["failure_category"] = "INVALID_CONFIGURATION"
        result["reason_code"] = "invalid_transport_configuration"
        return EXIT_INVALID_CONFIG, result

    result["mode"] = transport.mode

    if transport.mode != MODE_PROXY:
        result["failure_category"] = "INVALID_CONFIGURATION"
        result["reason_code"] = "direct_mode_rejected"
        return EXIT_INVALID_CONFIG, result

    result["proxy_listener_reachable"] = True

    executor = RequestExecutor(transport)

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                proxy=transport.playwright_proxy_config,
            )

            try:
                context = browser.new_context()

                try:
                    page = context.new_page()
                    page.set_default_timeout(transport.read_timeout_ms)
                    page.set_default_navigation_timeout(transport.connect_timeout_ms)

                    started_at = time.monotonic()
                    nav_result = executor.navigate(page, target_url)
                    result["latency_ms"] = round((time.monotonic() - started_at) * 1000)
                    result["status_code"] = nav_result.status_code

                    if nav_result.category != CATEGORY_SUCCESS:
                        result["neutral_request_success"] = False
                        result["failure_category"] = nav_result.category
                        result["reason_code"] = nav_result.reason_code
                        return EXIT_NEUTRAL_REQUEST_FAILED, result

                    result["neutral_request_success"] = True

                    try:
                        body_text = page.locator("body").inner_text(
                            timeout=transport.read_timeout_ms
                        )
                    except (PlaywrightError, PlaywrightTimeoutError):
                        result["tor_route_verified"] = False
                        result["failure_category"] = "TOR_ROUTE_VERIFICATION_FAILED"
                        result["reason_code"] = "body_read_failed"
                        return EXIT_TOR_ROUTE_VERIFICATION_FAILED, result

                    try:
                        observed_ip = parse_neutral_tor_response(body_text)
                    except TorRouteVerificationError as error:
                        result["tor_route_verified"] = False
                        result["failure_category"] = "TOR_ROUTE_VERIFICATION_FAILED"
                        result["reason_code"] = error.reason_code
                        return EXIT_TOR_ROUTE_VERIFICATION_FAILED, result

                    result["tor_route_verified"] = True
                    result["observed_exit_ip"] = observed_ip
                    result["ok"] = True
                    return EXIT_OK, result
                finally:
                    # Best-effort cleanup: a close() failure must never
                    # override an already-computed result/exit code above.
                    try:
                        context.close()
                    except Exception:
                        pass
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception:
        result["failure_category"] = result["failure_category"] or "INTERNAL_ERROR"
        result["reason_code"] = result["reason_code"] or "unexpected_exception"
        return EXIT_INTERNAL_ERROR, result


def main() -> int:
    exit_code, result = run_canary()
    print(json.dumps(result, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
