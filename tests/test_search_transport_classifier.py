"""Tests for scripts/search_transport/classifier.py -- pure, no network,
no Playwright, no PostgreSQL. Uses scripts/tor/local_http_simulator.py's
exception shapes (SimulatedTimeout/SimulatedConnectionFailure) as
stand-ins for real network failures, matching that module's own stated
boundary (generic HTTP-response-shape generation, no coupling to any
particular transport)."""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.search_transport.classifier import (
    CATEGORY_AUTH_CHALLENGE,
    CATEGORY_NETWORK_FAILURE,
    CATEGORY_RATE_LIMIT,
    CATEGORY_SUCCESS,
    classify_exception,
    classify_response,
)
from scripts.tor.local_http_simulator import SimulatedConnectionFailure, SimulatedTimeout


def test_200_is_success():
    result = classify_response(200)
    assert result.category == CATEGORY_SUCCESS
    assert result.status_code == 200


def test_3xx_is_success():
    result = classify_response(302)
    assert result.category == CATEGORY_SUCCESS


def test_429_is_rate_limit():
    result = classify_response(429)
    assert result.category == CATEGORY_RATE_LIMIT
    assert result.status_code == 429


def test_403_is_auth_challenge():
    result = classify_response(403)
    assert result.category == CATEGORY_AUTH_CHALLENGE
    assert result.status_code == 403


def test_dom_detected_auth_challenge_overrides_2xx_status():
    result = classify_response(200, auth_challenge_detected=True)
    assert result.category == CATEGORY_AUTH_CHALLENGE
    assert result.status_code == 200
    assert result.reason_code == "auth_challenge_detected"


def test_dom_detected_auth_challenge_overrides_429_too():
    # AUTH_CHALLENGE (an explicit, computed signal) takes precedence over
    # the raw status code -- a caller that detected a login wall knows
    # more than the bare status code does.
    result = classify_response(429, auth_challenge_detected=True)
    assert result.category == CATEGORY_AUTH_CHALLENGE


def test_500_is_network_failure():
    result = classify_response(500)
    assert result.category == CATEGORY_NETWORK_FAILURE
    assert result.status_code == 500
    assert result.reason_code == "http_500"


def test_timeout_exception_is_network_failure():
    try:
        raise SimulatedTimeout("simulated request timeout")
    except SimulatedTimeout as error:
        result = classify_exception(error)

    assert result.category == CATEGORY_NETWORK_FAILURE
    assert result.status_code is None
    assert result.reason_code == "timeout"


def test_connection_reset_exception_is_network_failure():
    try:
        raise SimulatedConnectionFailure("simulated connection failure")
    except SimulatedConnectionFailure as error:
        result = classify_exception(error)

    assert result.category == CATEGORY_NETWORK_FAILURE
    assert result.reason_code == "connection_reset"


def test_playwright_style_timeout_message_is_classified_as_timeout():
    result = classify_exception(Exception("Timeout 30000ms exceeded."))
    assert result.category == CATEGORY_NETWORK_FAILURE
    assert result.reason_code == "timeout"


def test_dns_failure_message_is_classified():
    result = classify_exception(Exception("net::ERR_NAME_NOT_RESOLVED at https://x"))
    assert result.reason_code == "dns_failure"


def test_proxy_failure_message_is_classified():
    result = classify_exception(Exception("net::ERR_PROXY_CONNECTION_FAILED"))
    assert result.reason_code == "proxy_unavailable"


def test_unknown_exception_falls_back_to_generic_network_failure():
    result = classify_exception(Exception("something completely unexpected"))
    assert result.category == CATEGORY_NETWORK_FAILURE
    assert result.reason_code == "unknown_network_error"


def test_invalid_category_is_rejected():
    import pytest

    from scripts.search_transport.classifier import RequestResult

    with pytest.raises(ValueError):
        RequestResult("NOT_A_REAL_CATEGORY", 200, "ok")
