"""Tests for scripts/search_transport_benchmark.py::aggregate_benchmark_results()
(Phase 3.4K Stabilization, Section 9) -- proves the benchmark reports real
per-request counts/rates, not the number of distinct failure CATEGORIES
observed (the bug this replaces: with only 4 possible categories,
"timeout_count" could never exceed 4 regardless of how many requests
actually timed out).

Pure/deterministic: constructs RequestResult values directly, no
Playwright, no browser, no network.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.search_transport.classifier import (
    CATEGORY_AUTH_CHALLENGE,
    CATEGORY_NETWORK_FAILURE,
    CATEGORY_RATE_LIMIT,
    CATEGORY_SUCCESS,
    RequestResult,
)
from scripts.search_transport_benchmark import aggregate_benchmark_results


def test_all_success():
    results = [RequestResult(CATEGORY_SUCCESS, 200, "ok") for _ in range(5)]
    latencies = [100.0, 100.0, 100.0, 100.0, 100.0]

    report = aggregate_benchmark_results("direct", results, latencies)

    assert report["requests"] == 5
    assert report["success_count"] == 5
    assert report["failure_count"] == 0
    assert report["timeout_count"] == 0
    assert report["success_rate"] == 1.0
    assert report["timeout_rate"] == 0.0


def test_timeout_count_reflects_actual_request_count_not_distinct_categories():
    # 10 requests: 7 real timeouts (same category, same reason_code), 3
    # successes. The pre-fix implementation counted distinct CATEGORIES
    # present (at most 2 here: SUCCESS and NETWORK_FAILURE) instead of
    # actual occurrences -- this proves the real count (7) is reported.
    results = (
        [RequestResult(CATEGORY_NETWORK_FAILURE, None, "timeout") for _ in range(7)]
        + [RequestResult(CATEGORY_SUCCESS, 200, "ok") for _ in range(3)]
    )
    latencies = [1.0] * 10

    report = aggregate_benchmark_results("direct", results, latencies)

    assert report["requests"] == 10
    assert report["success_count"] == 3
    assert report["failure_count"] == 7
    assert report["timeout_count"] == 7
    assert report["success_rate"] == 0.3
    assert report["timeout_rate"] == 0.7


def test_non_timeout_failures_count_toward_failure_but_not_timeout():
    # Distinguishes "failed" from "timed out": RATE_LIMIT and
    # AUTH_CHALLENGE are real failures with a real (non-"timeout")
    # reason_code -- they must inflate failure_count without inflating
    # timeout_count.
    results = [
        RequestResult(CATEGORY_SUCCESS, 200, "ok"),
        RequestResult(CATEGORY_RATE_LIMIT, 429, "http_429"),
        RequestResult(CATEGORY_AUTH_CHALLENGE, 403, "http_403"),
        RequestResult(CATEGORY_NETWORK_FAILURE, None, "dns_failure"),
        RequestResult(CATEGORY_NETWORK_FAILURE, None, "timeout"),
    ]
    latencies = [1.0] * 5

    report = aggregate_benchmark_results("proxy", results, latencies)

    assert report["requests"] == 5
    assert report["success_count"] == 1
    assert report["failure_count"] == 4
    assert report["timeout_count"] == 1
    assert report["timeout_rate"] == 0.2
    assert report["failure_category_counts"] == {
        CATEGORY_SUCCESS: 1,
        CATEGORY_RATE_LIMIT: 1,
        CATEGORY_AUTH_CHALLENGE: 1,
        CATEGORY_NETWORK_FAILURE: 2,
    }


def test_zero_requests_does_not_divide_by_zero():
    report = aggregate_benchmark_results("direct", [], [])

    assert report["requests"] == 0
    assert report["success_rate"] == 0.0
    assert report["timeout_rate"] == 0.0
    assert report["p50_latency_ms"] is None
    assert report["p95_latency_ms"] is None
    assert report["mean_latency_ms"] is None


def test_latency_percentiles_and_mean():
    results = [RequestResult(CATEGORY_SUCCESS, 200, "ok") for _ in range(4)]
    latencies = [100.0, 200.0, 300.0, 400.0]

    report = aggregate_benchmark_results("direct", results, latencies)

    assert report["mean_latency_ms"] == 250.0
    # sorted: [100, 200, 300, 400]; p50 index = int(4*0.5)=2 -> 300
    assert report["p50_latency_ms"] == 300.0
    # p95 index = int(4*0.95)=3 -> 400
    assert report["p95_latency_ms"] == 400.0


def test_mode_is_passed_through():
    report = aggregate_benchmark_results("proxy", [], [])
    assert report["mode"] == "proxy"
