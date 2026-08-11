"""Tests for scripts/tor/verify_tor_connectivity.py's response validation.

check_exit_ip() must never declare success merely because an IP-shaped
string exists in the response body -- a manual/direct (non-Tor) endpoint
must never be mistaken for successful Tor verification. These tests
exercise parse_tor_ip_check_response(), the pure parsing/validation
function factored out of check_exit_ip() specifically so this can be
tested without Playwright or a real network call. No LinkedIn, no Tor
process, no browser.
"""
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.tor.verify_tor_connectivity import (
    TorVerificationError,
    parse_tor_ip_check_response,
)


def test_valid_is_tor_true_and_ip_succeeds():
    body = json.dumps({"IsTor": True, "IP": "198.51.100.7"})
    assert parse_tor_ip_check_response(body) == "198.51.100.7"


def test_is_tor_false_fails():
    body = json.dumps({"IsTor": False, "IP": "198.51.100.7"})

    with pytest.raises(TorVerificationError, match="IsTor=False"):
        parse_tor_ip_check_response(body)


def test_missing_is_tor_key_fails():
    body = json.dumps({"IP": "198.51.100.7"})

    with pytest.raises(TorVerificationError, match="IsTor=None"):
        parse_tor_ip_check_response(body)


def test_is_tor_truthy_but_not_boolean_true_fails():
    """IsTor must be exactly True, not merely truthy -- guards against a
    future endpoint change that returns "true" (string) or 1 (int)."""
    for truthy_non_bool in ["true", 1, "yes"]:
        body = json.dumps({"IsTor": truthy_non_bool, "IP": "198.51.100.7"})

        with pytest.raises(TorVerificationError):
            parse_tor_ip_check_response(body)


def test_missing_ip_fails():
    body = json.dumps({"IsTor": True})

    with pytest.raises(TorVerificationError, match="usable IP"):
        parse_tor_ip_check_response(body)


def test_empty_ip_fails():
    body = json.dumps({"IsTor": True, "IP": ""})

    with pytest.raises(TorVerificationError, match="usable IP"):
        parse_tor_ip_check_response(body)


def test_non_string_ip_fails():
    body = json.dumps({"IsTor": True, "IP": 12345})

    with pytest.raises(TorVerificationError, match="usable IP"):
        parse_tor_ip_check_response(body)


def test_invalid_json_fails():
    with pytest.raises(TorVerificationError, match="did not return valid JSON"):
        parse_tor_ip_check_response("<html>not json</html>")


def test_empty_body_fails():
    with pytest.raises(TorVerificationError, match="did not return valid JSON"):
        parse_tor_ip_check_response("")


def test_json_array_instead_of_object_fails():
    """A manual/direct endpoint could return valid but wrongly-shaped
    JSON (e.g. a bare array) -- must fail, not be silently coerced."""
    with pytest.raises(TorVerificationError, match="non-object JSON"):
        parse_tor_ip_check_response(json.dumps(["not", "an", "object"]))


def test_a_plausible_looking_html_body_never_becomes_a_fake_ip():
    """Regression guard for the exact defect described in the audit: the
    old implementation fell back to returning body_text.strip() as the
    "IP" whenever JSON parsing failed, so a manual/direct non-Tor
    endpoint's HTML error page could be mistaken for a successful Tor
    verification. That fallback must be gone."""
    non_json_body_that_looks_like_it_could_contain_an_ip = "Your IP is 203.0.113.5"

    with pytest.raises(TorVerificationError):
        parse_tor_ip_check_response(non_json_body_that_looks_like_it_could_contain_an_ip)
