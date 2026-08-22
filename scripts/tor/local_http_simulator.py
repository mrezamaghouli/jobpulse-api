"""Deterministic LOCAL HTTP-response-shape generator for integration
testing Phase 2 Tor control-plane code paths (scripts/tor/
circuit_manager.py, scripts/tor/observability.py) without a real
network call, a real Tor process, or any real target.

STRICT BOUNDARY (Phase 2 design, section K):
  - This module is a GENERIC HTTP-response-shape generator ONLY. It
    contains no LinkedIn URLs, no LinkedIn selectors, no LinkedIn
    cookies/headers, no browser fingerprint behavior, and no CAPTCHA
    behavior -- and must never gain any of those.
  - It has NO connection of any kind to rotate_circuit()/
    request_new_identity()/NEWNYM: nothing in this module calls,
    imports, or is imported by scripts/tor/circuit_manager.py. Producing
    a 429 or 403 here triggers nothing automatically. A test that wants
    to exercise "a verification failed" must inject its OWN callable
    (shaped like a real verify_fn: takes no arguments, returns an exit
    IP string, or raises) into verify_circuit()/rotate_circuit()
    explicitly -- this module never calls circuit_manager itself, and
    must never be extended to do so. See tests/test_tor_local_simulator.py
    (this module's own sequencing tests, with no circuit_manager
    involvement) versus tests/test_tor_circuit_manager.py (manual
    rotation/verification tests, which use a plain injected verify_fn,
    not this simulator, precisely to keep those two concerns from
    becoming coupled).
  - Deterministic: given the same constructor arguments, this always
    produces the exact same sequence of outcomes -- no randomness, no
    wall-clock dependency for "recovery" (see rate_limited_after()'s
    cooldown_calls, which counts CALLS, not elapsed time) -- so tests
    built on it are fully reproducible.
"""
from dataclasses import dataclass


class SimulatedTimeout(Exception):
    """Raised by a simulator step to emulate a request timing out."""


class SimulatedConnectionFailure(Exception):
    """Raised by a simulator step to emulate a connection-level failure
    (e.g. the SOCKS proxy or target refusing/resetting the connection)."""


@dataclass(frozen=True)
class SimulatedResponse:
    status_code: int
    body: str
    latency_seconds: float = 0.0


def step_success(latency_seconds: float = 0.0, body: str = '{"ok": true}'):
    """Returns a zero-arg callable step producing a 200 response. This
    module never itself calls time.sleep() -- latency_seconds is metadata
    a caller/test may choose to act on, keeping unit tests fast by
    default."""
    def _step():
        return SimulatedResponse(status_code=200, body=body, latency_seconds=latency_seconds)
    return _step


def step_429(latency_seconds: float = 0.0):
    def _step():
        return SimulatedResponse(
            status_code=429, body='{"error": "rate_limited"}', latency_seconds=latency_seconds,
        )
    return _step


def step_403(latency_seconds: float = 0.0):
    def _step():
        return SimulatedResponse(
            status_code=403, body='{"error": "forbidden"}', latency_seconds=latency_seconds,
        )
    return _step


def step_malformed(latency_seconds: float = 0.0):
    """A 200 whose body is not valid JSON -- exercises response-parsing
    failure paths distinct from a non-2xx status code."""
    def _step():
        return SimulatedResponse(status_code=200, body="not-json{{{", latency_seconds=latency_seconds)
    return _step


def step_timeout():
    def _step():
        raise SimulatedTimeout("simulated request timeout")
    return _step


def step_connection_failure():
    def _step():
        raise SimulatedConnectionFailure("simulated connection failure")
    return _step


class LocalHttpSimulator:
    """A deterministic sequence of steps, indexed by call number: the
    Nth call to next_response() runs steps[N-1] if provided, else
    default_step. Every step is a zero-arg callable that either returns
    a SimulatedResponse or raises (SimulatedTimeout/
    SimulatedConnectionFailure, or any exception a caller-supplied step
    chooses to raise)."""

    def __init__(self, steps=None, default_step=None):
        self._steps = list(steps) if steps is not None else []
        self._default_step = default_step if default_step is not None else step_success()
        self._call_count = 0

    @property
    def call_count(self) -> int:
        return self._call_count

    def next_response(self) -> SimulatedResponse:
        self._call_count += 1
        index = self._call_count - 1
        step = self._steps[index] if index < len(self._steps) else self._default_step
        return step()


def always_success(latency_seconds: float = 0.0) -> LocalHttpSimulator:
    return LocalHttpSimulator(default_step=step_success(latency_seconds))


def always_403() -> LocalHttpSimulator:
    return LocalHttpSimulator(default_step=step_403())


def always_timeout() -> LocalHttpSimulator:
    return LocalHttpSimulator(default_step=step_timeout())


def always_connection_failure() -> LocalHttpSimulator:
    return LocalHttpSimulator(default_step=step_connection_failure())


def always_malformed() -> LocalHttpSimulator:
    return LocalHttpSimulator(default_step=step_malformed())


def rate_limited_after(n: int, cooldown_calls: int = 0) -> LocalHttpSimulator:
    """First n calls succeed. Then: if cooldown_calls == 0, 429 forever
    after that. If cooldown_calls > 0, exactly cooldown_calls worth of
    429s follow before recovering to success again -- a LOCAL,
    call-counted stand-in for "the simulated target eventually allows
    requests again," never tied to a real clock, so tests never sleep
    to observe recovery."""
    if n < 0:
        raise ValueError("n must be >= 0")
    if cooldown_calls < 0:
        raise ValueError("cooldown_calls must be >= 0")

    steps = [step_success() for _ in range(n)]

    if cooldown_calls > 0:
        steps += [step_429() for _ in range(cooldown_calls)]
        return LocalHttpSimulator(steps=steps, default_step=step_success())

    return LocalHttpSimulator(steps=steps, default_step=step_429())
