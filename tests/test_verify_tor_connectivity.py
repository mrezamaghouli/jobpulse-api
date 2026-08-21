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
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import scripts.tor.verify_tor_connectivity as vtc
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


# =====================================================================
# check_bootstrap_status(): bootstrap_started/ready/failed events
#
# Controller (stem) and psycopg2 are ALWAYS mocked in this section --
# these are unit tests and must never contact a real Tor process or a
# real database. The actual persistence/event-emission LOGIC (schema,
# allowlist, transactional retention) is separately, directly unit- and
# PG16-integration-tested in tests/test_tor_circuit_manager.py and
# tests/test_tor_circuit_manager_postgres_integration.py; these tests
# only prove check_bootstrap_status() calls the right
# record_bootstrap_* function, with the right arguments, on each path.
# =====================================================================

class _FakeConnection:
    def close(self):
        pass


def _controller_context_manager(controller_mock):
    context = mock.MagicMock()
    context.__enter__.return_value = controller_mock
    context.__exit__.return_value = False
    return context


def test_check_bootstrap_status_emits_started_then_ready_on_success(monkeypatch):
    monkeypatch.setenv("TOR_CONTROL_HOST", "127.0.0.1")
    monkeypatch.setenv("TOR_CONTROL_PORT", "9051")

    fake_controller = mock.MagicMock()
    fake_controller.get_info.return_value = "PROGRESS=100 TAG=done SUMMARY=Done"

    with mock.patch.object(vtc, "psycopg2") as fake_psycopg2, \
         mock.patch.object(vtc, "Controller") as fake_controller_cls, \
         mock.patch.object(vtc, "record_bootstrap_started") as fake_started, \
         mock.patch.object(vtc, "record_bootstrap_ready") as fake_ready, \
         mock.patch.object(vtc, "record_bootstrap_failed") as fake_failed:
        fake_psycopg2.connect.return_value = _FakeConnection()
        fake_controller_cls.from_port.return_value = _controller_context_manager(fake_controller)

        phase = vtc.check_bootstrap_status()

    assert "PROGRESS=100" in phase
    assert fake_started.called, "bootstrap_started must be emitted when verification begins"
    assert fake_ready.called, "bootstrap_ready must be emitted only once progress is confirmed at 100%"
    assert not fake_failed.called


def test_check_bootstrap_status_emits_failed_on_control_port_exception(monkeypatch):
    monkeypatch.setenv("TOR_CONTROL_HOST", "127.0.0.1")
    monkeypatch.setenv("TOR_CONTROL_PORT", "9051")

    with mock.patch.object(vtc, "psycopg2") as fake_psycopg2, \
         mock.patch.object(vtc, "Controller") as fake_controller_cls, \
         mock.patch.object(vtc, "record_bootstrap_started") as fake_started, \
         mock.patch.object(vtc, "record_bootstrap_ready") as fake_ready, \
         mock.patch.object(vtc, "record_bootstrap_failed") as fake_failed:
        fake_psycopg2.connect.return_value = _FakeConnection()
        fake_controller_cls.from_port.side_effect = RuntimeError("control port unreachable")

        with pytest.raises(RuntimeError, match="control port unreachable"):
            vtc.check_bootstrap_status()

    assert fake_started.called
    assert not fake_ready.called
    assert fake_failed.called
    assert fake_failed.call_args.args[-1] == vtc.ERROR_CATEGORY_CONTROL_PORT_FAILURE


def test_check_bootstrap_status_emits_failed_when_progress_not_100(monkeypatch):
    monkeypatch.setenv("TOR_CONTROL_HOST", "127.0.0.1")
    monkeypatch.setenv("TOR_CONTROL_PORT", "9051")

    fake_controller = mock.MagicMock()
    fake_controller.get_info.return_value = "PROGRESS=50 TAG=handshake SUMMARY=Handshaking"

    with mock.patch.object(vtc, "psycopg2") as fake_psycopg2, \
         mock.patch.object(vtc, "Controller") as fake_controller_cls, \
         mock.patch.object(vtc, "record_bootstrap_started") as fake_started, \
         mock.patch.object(vtc, "record_bootstrap_ready") as fake_ready, \
         mock.patch.object(vtc, "record_bootstrap_failed") as fake_failed:
        fake_psycopg2.connect.return_value = _FakeConnection()
        fake_controller_cls.from_port.return_value = _controller_context_manager(fake_controller)

        with pytest.raises(RuntimeError, match="has not finished bootstrapping"):
            vtc.check_bootstrap_status()

    assert fake_started.called
    assert not fake_ready.called, "bootstrap_ready must NEVER be emitted below PROGRESS=100"
    assert fake_failed.called
    assert fake_failed.call_args.args[-1] == vtc.ERROR_CATEGORY_BOOTSTRAP_INCOMPLETE


def test_check_bootstrap_status_closes_connection_on_failure_path():
    fake_conn = mock.MagicMock()

    with mock.patch.object(vtc, "psycopg2") as fake_psycopg2, \
         mock.patch.object(vtc, "Controller") as fake_controller_cls, \
         mock.patch.object(vtc, "record_bootstrap_started"), \
         mock.patch.object(vtc, "record_bootstrap_failed"):
        fake_psycopg2.connect.return_value = fake_conn
        fake_controller_cls.from_port.side_effect = RuntimeError("boom")

        with pytest.raises(RuntimeError):
            vtc.check_bootstrap_status()

    assert fake_conn.close.called


def test_check_bootstrap_status_error_categories_are_normalized_not_raw_text(monkeypatch):
    """The record_bootstrap_failed error_category argument must always
    be one of the fixed, normalized categories -- never the raw
    exception message, which could (in principle) contain
    implementation detail beyond what's safe to persist."""
    monkeypatch.setenv("TOR_CONTROL_HOST", "127.0.0.1")
    monkeypatch.setenv("TOR_CONTROL_PORT", "9051")

    with mock.patch.object(vtc, "psycopg2") as fake_psycopg2, \
         mock.patch.object(vtc, "Controller") as fake_controller_cls, \
         mock.patch.object(vtc, "record_bootstrap_started"), \
         mock.patch.object(vtc, "record_bootstrap_failed") as fake_failed:
        fake_psycopg2.connect.return_value = _FakeConnection()
        fake_controller_cls.from_port.side_effect = RuntimeError(
            "some very specific, potentially sensitive control-port error detail"
        )

        with pytest.raises(RuntimeError):
            vtc.check_bootstrap_status()

    category = fake_failed.call_args.args[-1]
    assert category == vtc.ERROR_CATEGORY_CONTROL_PORT_FAILURE
    assert "sensitive" not in category
    assert "specific" not in category


def test_check_bootstrap_status_never_contacts_a_real_tor_process(monkeypatch):
    """Regression guard: Controller must always be mocked in this test
    file. Confirms the mock actually intercepted the class (from_port
    was called on the MOCK, not a real stem.control.Controller)."""
    monkeypatch.setenv("TOR_CONTROL_HOST", "127.0.0.1")
    monkeypatch.setenv("TOR_CONTROL_PORT", "9051")

    with mock.patch.object(vtc, "psycopg2") as fake_psycopg2, \
         mock.patch.object(vtc, "Controller") as fake_controller_cls, \
         mock.patch.object(vtc, "record_bootstrap_started"), \
         mock.patch.object(vtc, "record_bootstrap_failed"):
        fake_psycopg2.connect.return_value = _FakeConnection()
        fake_controller_cls.from_port.side_effect = RuntimeError("mocked -- never a real connection")

        with pytest.raises(RuntimeError, match="mocked -- never a real connection"):
            vtc.check_bootstrap_status()

    assert fake_controller_cls.from_port.called
