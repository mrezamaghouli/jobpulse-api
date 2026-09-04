"""Tests for scripts/search_transport/metrics.py -- Section 9/Security
guarantee: secrets, cookies, and session headers can never reach a log
line through this module, because field NAMES are restricted to a fixed
allowlist (an unknown field name raises rather than being silently
logged)."""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.search_transport.metrics import record_metric


def test_allowed_fields_are_logged(capsys):
    record_metric("search_transport_request", mode="direct", category="SUCCESS", latency_ms=123)

    captured = capsys.readouterr()
    assert "SEARCH_TRANSPORT_METRIC" in captured.out
    assert '"mode": "direct"' in captured.out
    assert '"category": "SUCCESS"' in captured.out


def test_password_field_is_rejected():
    with pytest.raises(ValueError):
        record_metric("tor_circuit_rotation_requested", password="hunter2")


def test_cookie_field_is_rejected():
    with pytest.raises(ValueError):
        record_metric("search_transport_request", cookie="session=abc123")


def test_raw_exception_text_field_is_rejected():
    with pytest.raises(ValueError):
        record_metric("search_transport_request", raw_error="Traceback (most recent call last)...")


def test_control_port_password_never_reaches_stdout_even_if_attempted(capsys):
    try:
        record_metric("tor_circuit_rotation_requested", control_password="super-secret-value")
    except ValueError:
        pass

    captured = capsys.readouterr()
    assert "super-secret-value" not in captured.out
