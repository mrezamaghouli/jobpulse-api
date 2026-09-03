"""Queue retry-budget policy (Phase 3.4K, Section 8).

Pure, side-effect-free helpers used by
scripts/process_search_demand_queue.py to stop a failing search-demand
row from re-entering the queue forever. Before this phase,
mark_targets(ids, "failed", ...) always set status back to 'pending' and
incremented fail_count with no cap -- an unbounded retry loop. This module
is the fix: once a row's fail_count reaches the configured budget, it
moves to a NEW terminal 'failed' status instead (job_search_demand_queue
already had a fail_count column and a 'pending'/'running'/'done' status
vocabulary; 'failed' as an actual terminal status value is new).
fetch_pending_targets() already filters `WHERE status = 'pending'`, so a
row in terminal 'failed' state is automatically excluded from being
picked up again -- no other query needs to change.
"""
from dataclasses import dataclass

from app.config import get_search_demand_max_attempts


STATUS_PENDING = "pending"
STATUS_FAILED = "failed"


@dataclass(frozen=True)
class RetryDecision:
    next_status: str
    attempts_used: int
    max_attempts: int

    @property
    def exhausted(self) -> bool:
        return self.next_status == STATUS_FAILED


def decide_retry(fail_count_before: int, max_attempts: int = None) -> RetryDecision:
    """fail_count_before is the row's fail_count BEFORE this failure is
    recorded. Returns whether the row should go back to 'pending' (budget
    remains) or move to terminal 'failed' (budget exhausted) once this
    failure's increment is applied."""
    if max_attempts is None:
        max_attempts = get_search_demand_max_attempts()

    attempts_used = fail_count_before + 1
    next_status = STATUS_FAILED if attempts_used >= max_attempts else STATUS_PENDING

    return RetryDecision(next_status, attempts_used, max_attempts)
