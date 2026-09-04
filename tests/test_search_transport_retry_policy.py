"""Tests for scripts/search_transport/retry_policy.py -- pure functions,
no database. Proves the retry budget actually terminates instead of
requeuing forever."""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.search_transport.retry_policy import STATUS_FAILED, STATUS_PENDING, decide_retry


def test_first_failure_requeues_to_pending():
    decision = decide_retry(fail_count_before=0, max_attempts=3)

    assert decision.next_status == STATUS_PENDING
    assert decision.exhausted is False
    assert decision.attempts_used == 1


def test_failure_below_budget_still_requeues():
    decision = decide_retry(fail_count_before=1, max_attempts=3)

    assert decision.next_status == STATUS_PENDING
    assert decision.attempts_used == 2


def test_failure_reaching_budget_is_terminal():
    decision = decide_retry(fail_count_before=2, max_attempts=3)

    assert decision.next_status == STATUS_FAILED
    assert decision.exhausted is True
    assert decision.attempts_used == 3


def test_failure_past_budget_stays_terminal():
    # Defensive: a row somehow already above budget (e.g. max_attempts was
    # lowered after some failures were recorded) must never be requeued.
    decision = decide_retry(fail_count_before=10, max_attempts=3)

    assert decision.exhausted is True


def test_max_attempts_of_one_never_retries():
    decision = decide_retry(fail_count_before=0, max_attempts=1)

    assert decision.exhausted is True


def test_default_max_attempts_comes_from_config(monkeypatch):
    monkeypatch.setenv("SEARCH_DEMAND_MAX_ATTEMPTS", "5")

    decision = decide_retry(fail_count_before=3)

    assert decision.max_attempts == 5
    assert decision.next_status == STATUS_PENDING

    decision = decide_retry(fail_count_before=4)

    assert decision.next_status == STATUS_FAILED


def test_a_row_can_never_loop_forever():
    # Simulates repeated failures of the same row and proves it reaches a
    # terminal state within max_attempts failures, never cycling back to
    # pending indefinitely.
    max_attempts = 3
    fail_count = 0

    for _ in range(50):
        decision = decide_retry(fail_count_before=fail_count, max_attempts=max_attempts)
        fail_count = decision.attempts_used

        if decision.exhausted:
            break
    else:
        raise AssertionError("row was never marked terminal -- infinite retry loop")

    assert fail_count <= max_attempts
