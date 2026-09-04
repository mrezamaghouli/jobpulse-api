"""Observability for the search transport layer (Phase 3.4K, Section 9).

Structured, greppable stdout lines -- matching the existing convention
already used across this codebase (e.g. collector_postgres.py's
"COLLECTOR_RESULT ..." line). No new database table: this phase adds
measurement capability, not a metrics store: metrics.py's job is to make
transport/classification/Tor/queue events observable in logs, cheaply and
without a schema migration.

Field names are restricted to a fixed allowlist -- exactly the same
discipline scripts/tor/circuit_manager.py's emit_event()/
_ALLOWED_EVENT_DETAIL_FIELDS already applies -- so a caller cannot smuggle
a password, cookie, or raw exception text into a log line just because it
was passed as a kwarg. An unknown field name raises rather than being
silently dropped or silently logged: a caller-side bug (e.g. accidentally
passing `cookie=...`) must fail loudly, not leak quietly.
"""
import json


_ALLOWED_METRIC_FIELDS = {
    "mode",
    "category",
    "status_code",
    "latency_ms",
    "reason",
    "url_host",
    "circuit_key",
    "rotation_outcome",
    "fail_count",
    "max_attempts",
    "queue_status",
    "attempt_count",
    "target",
}


def record_metric(event: str, **fields) -> None:
    unknown_fields = set(fields) - _ALLOWED_METRIC_FIELDS

    if unknown_fields:
        raise ValueError(f"Unsupported search transport metric field(s): {sorted(unknown_fields)}")

    payload = {"event": event, **fields}
    print(f"SEARCH_TRANSPORT_METRIC {json.dumps(payload, sort_keys=True)}")
