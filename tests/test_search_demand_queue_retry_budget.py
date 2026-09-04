"""Tests for the retry-budget wiring in
scripts/process_search_demand_queue.py::mark_targets() (Phase 3.4K,
Section 8). Uses a fake psycopg2 connection/cursor -- no real database --
to prove:
  - a row below its retry budget is requeued to 'pending'
  - a row that reaches its retry budget is moved to the TERMINAL 'failed'
    status instead (closing the previously-unbounded retry loop)
  - a failed task is never marked 'done'
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import scripts.process_search_demand_queue as queue_module


class FakeCursor:
    def __init__(self, fail_counts_by_id):
        self._fail_counts_by_id = fail_counts_by_id
        self.executed = []
        self._last_select_ids = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql.strip(), params))

        if sql.strip().startswith("SELECT id, fail_count"):
            ids = params[0]
            self._last_select_ids = ids

    def fetchall(self):
        return [
            (task_id, self._fail_counts_by_id.get(task_id, 0))
            for task_id in self._last_select_ids
        ]


class FakeConnection:
    def __init__(self, fail_counts_by_id):
        self.cursor_obj = FakeCursor(fail_counts_by_id)
        self.committed = False
        self.closed = False

    def cursor(self, cursor_factory=None):
        return self.cursor_obj

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


def _updates_targeting_status(cursor, status):
    return [
        (sql, params) for sql, params in cursor.executed
        if sql.startswith("UPDATE job_search_demand_queue") and f"status = '{status}'" in sql
    ]


def test_row_below_budget_is_requeued_to_pending(monkeypatch):
    monkeypatch.setenv("SEARCH_DEMAND_MAX_ATTEMPTS", "3")

    fake_conn = FakeConnection(fail_counts_by_id={101: 0})
    monkeypatch.setattr(queue_module.psycopg2, "connect", lambda **kwargs: fake_conn)

    queue_module.mark_targets([101], "failed", error="boom")

    pending_updates = _updates_targeting_status(fake_conn.cursor_obj, "pending")
    failed_updates = _updates_targeting_status(fake_conn.cursor_obj, "failed")

    assert len(pending_updates) == 1
    assert pending_updates[0][1][-1] == [101]
    assert len(failed_updates) == 0
    assert fake_conn.committed is True


def test_row_reaching_budget_is_moved_to_terminal_failed(monkeypatch):
    monkeypatch.setenv("SEARCH_DEMAND_MAX_ATTEMPTS", "3")

    fake_conn = FakeConnection(fail_counts_by_id={202: 2})
    monkeypatch.setattr(queue_module.psycopg2, "connect", lambda **kwargs: fake_conn)

    queue_module.mark_targets([202], "failed", error="boom again")

    pending_updates = _updates_targeting_status(fake_conn.cursor_obj, "pending")
    failed_updates = _updates_targeting_status(fake_conn.cursor_obj, "failed")

    assert len(pending_updates) == 0
    assert len(failed_updates) == 1
    assert failed_updates[0][1][-1] == [202]


def test_failed_task_is_never_marked_done(monkeypatch):
    monkeypatch.setenv("SEARCH_DEMAND_MAX_ATTEMPTS", "3")

    fake_conn = FakeConnection(fail_counts_by_id={303: 5})
    monkeypatch.setattr(queue_module.psycopg2, "connect", lambda **kwargs: fake_conn)

    queue_module.mark_targets([303], "failed", error="permanent failure")

    done_updates = [
        (sql, params) for sql, params in fake_conn.cursor_obj.executed
        if sql.startswith("UPDATE job_search_demand_queue") and "status = 'done'" in sql
    ]

    assert len(done_updates) == 0


def test_mixed_batch_splits_rows_by_remaining_budget(monkeypatch):
    monkeypatch.setenv("SEARCH_DEMAND_MAX_ATTEMPTS", "3")

    fake_conn = FakeConnection(fail_counts_by_id={1: 0, 2: 2, 3: 1})
    monkeypatch.setattr(queue_module.psycopg2, "connect", lambda **kwargs: fake_conn)

    queue_module.mark_targets([1, 2, 3], "failed", error="mixed")

    pending_updates = _updates_targeting_status(fake_conn.cursor_obj, "pending")
    failed_updates = _updates_targeting_status(fake_conn.cursor_obj, "failed")

    assert set(pending_updates[0][1][-1]) == {1, 3}
    assert set(failed_updates[0][1][-1]) == {2}
