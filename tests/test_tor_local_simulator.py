"""Tests for scripts/tor/local_http_simulator.py -- a generic, LOCAL,
deterministic HTTP-response-shape generator with NO connection to
scripts/tor/circuit_manager.py.

CRITICAL BOUNDARY under test (Phase 2 design, section K): a 429/403/
timeout/connection-failure produced by this simulator must never, by
itself, trigger rotate_circuit(), request_new_identity(), or any
automatic NEWNYM logic. This file proves the simulator's OWN
deterministic sequencing in complete isolation -- it never imports or
calls anything from scripts/tor/circuit_manager.py. The separate "429/
403/timeout does not trigger rotation" boundary is proven in
tests/test_tor_circuit_manager.py using a Tor-verification-shaped
injected verify_fn built from this simulator's output, calling
verify_circuit() explicitly and asserting request_new_identity was
never invoked -- never by any implicit wiring in this module.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest

from scripts.tor.local_http_simulator import (
    LocalHttpSimulator,
    SimulatedConnectionFailure,
    SimulatedTimeout,
    always_403,
    always_connection_failure,
    always_malformed,
    always_success,
    always_timeout,
    rate_limited_after,
    step_403,
    step_429,
    step_connection_failure,
    step_malformed,
    step_success,
    step_timeout,
)


# =====================================================================
# No LinkedIn/target-specific content anywhere in this module
# =====================================================================

def test_simulator_module_contains_no_linkedin_artifacts():
    """The module's own docstring legitimately DISCUSSES the boundary
    ("no LinkedIn URLs...") in prose -- so this checks for concrete,
    LinkedIn-SHAPED artifacts (an actual domain, a cookie/header name,
    a CSS-selector-looking string) rather than the bare word, which
    would also (correctly) flag the explanatory comment itself."""
    import scripts.tor.local_http_simulator as sim_module
    source = Path(sim_module.__file__).read_text().lower()

    for forbidden in ("linkedin.com", "li_at", "user-agent:", ".jobs-search", "jsessionid"):
        assert forbidden not in source, forbidden


def test_simulator_module_never_imports_or_calls_circuit_manager():
    """AST-based, not a raw substring search -- the module's own
    docstring legitimately CROSS-REFERENCES
    scripts/tor/circuit_manager.py by name in prose, which a plain
    substring check would incorrectly flag. This instead inspects the
    actual parsed import statements and call expressions."""
    import ast

    import scripts.tor.local_http_simulator as sim_module
    source = Path(sim_module.__file__).read_text()
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "circuit_manager" not in alias.name
        if isinstance(node, ast.ImportFrom):
            assert not (node.module and "circuit_manager" in node.module)
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            assert name not in ("request_new_identity", "rotate_circuit", "verify_circuit")


# =====================================================================
# Individual step factories
# =====================================================================

def test_step_success_returns_200():
    response = step_success()()
    assert response.status_code == 200
    assert response.body == '{"ok": true}'


def test_step_success_custom_body_and_latency():
    response = step_success(latency_seconds=0.5, body='{"custom": 1}')()
    assert response.status_code == 200
    assert response.body == '{"custom": 1}'
    assert response.latency_seconds == 0.5


def test_step_429_returns_429():
    assert step_429()().status_code == 429


def test_step_403_returns_403():
    assert step_403()().status_code == 403


def test_step_malformed_returns_200_with_invalid_json_body():
    response = step_malformed()()
    assert response.status_code == 200
    import json
    with pytest.raises(json.JSONDecodeError):
        json.loads(response.body)


def test_step_timeout_raises():
    with pytest.raises(SimulatedTimeout):
        step_timeout()()


def test_step_connection_failure_raises():
    with pytest.raises(SimulatedConnectionFailure):
        step_connection_failure()()


# =====================================================================
# LocalHttpSimulator: deterministic sequencing
# =====================================================================

def test_simulator_is_deterministic_across_independent_instances():
    sim1 = rate_limited_after(3, cooldown_calls=2)
    sim2 = rate_limited_after(3, cooldown_calls=2)

    seq1 = [sim1.next_response().status_code for _ in range(8)]
    seq2 = [sim2.next_response().status_code for _ in range(8)]

    assert seq1 == seq2


def test_simulator_uses_explicit_steps_then_falls_back_to_default():
    sim = LocalHttpSimulator(steps=[step_429(), step_403()], default_step=step_success())

    assert sim.next_response().status_code == 429
    assert sim.next_response().status_code == 403
    assert sim.next_response().status_code == 200
    assert sim.next_response().status_code == 200


def test_simulator_tracks_call_count():
    sim = always_success()
    assert sim.call_count == 0
    sim.next_response()
    sim.next_response()
    assert sim.call_count == 2


# =====================================================================
# rate_limited_after: 429-after-N and recovery-after-local-cooldown
# =====================================================================

def test_rate_limited_after_n_succeeds_then_429_forever():
    sim = rate_limited_after(2)
    results = [sim.next_response().status_code for _ in range(5)]
    assert results == [200, 200, 429, 429, 429]


def test_rate_limited_after_recovers_after_configured_cooldown_calls():
    sim = rate_limited_after(2, cooldown_calls=3)
    results = [sim.next_response().status_code for _ in range(7)]
    assert results == [200, 200, 429, 429, 429, 200, 200]


def test_rate_limited_after_rejects_negative_n():
    with pytest.raises(ValueError):
        rate_limited_after(-1)


def test_rate_limited_after_rejects_negative_cooldown():
    with pytest.raises(ValueError):
        rate_limited_after(1, cooldown_calls=-1)


def test_rate_limited_after_zero_is_429_immediately():
    sim = rate_limited_after(0)
    assert sim.next_response().status_code == 429


# =====================================================================
# always_* convenience constructors
# =====================================================================

def test_always_success_never_varies():
    sim = always_success()
    for _ in range(10):
        assert sim.next_response().status_code == 200


def test_always_403_never_varies():
    sim = always_403()
    for _ in range(5):
        assert sim.next_response().status_code == 403


def test_always_timeout_always_raises():
    sim = always_timeout()
    for _ in range(3):
        with pytest.raises(SimulatedTimeout):
            sim.next_response()


def test_always_connection_failure_always_raises():
    sim = always_connection_failure()
    for _ in range(3):
        with pytest.raises(SimulatedConnectionFailure):
            sim.next_response()


def test_always_malformed_never_varies():
    sim = always_malformed()
    for _ in range(3):
        response = sim.next_response()
        assert response.status_code == 200
        assert response.body == "not-json{{{"
