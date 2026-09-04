"""Direct vs. proxy transport benchmark (Phase 3.4K, Section 10).

Measures the SAME thing production collection does -- a real Playwright
Chromium navigation through scripts/search_transport/executor.py -- for
both transport modes, and reports p50/p95 latency, success rate, and
timeout rate for each. This exists to let an operator PROVE whether proxy
transport helps or hurts before any production rollout decision is made;
it does not assume an answer, and it changes nothing in production on its
own (it is a standalone script, never imported by collector code).

Safety: the default target is TOR_IP_CHECK_URL (the same neutral,
non-LinkedIn endpoint scripts/tor/verify_tor_connectivity.py already uses
to confirm Tor connectivity) -- never LinkedIn. Pointing --target-url at a
real LinkedIn URL is an explicit operator choice made with full knowledge
of LinkedIn's rate limits and this account's authorization; this script
never defaults to that.

Usage:
    python -m scripts.search_transport_benchmark --modes direct,proxy --requests 10
    python -m scripts.search_transport_benchmark --modes direct --requests 5 --target-url https://example.com

Throughput metrics this script does NOT cover (jobs discovered/minute,
queue processing duration) require a full collection cycle against real
LinkedIn queries and are an operator-triggered exercise documented in the
Phase 3.4K report, not something safe to automate here.
"""
import argparse
import statistics
import sys
import time

from playwright.sync_api import sync_playwright

from app.config import get_tor_ip_check_url
from scripts.search_transport.classifier import CATEGORY_SUCCESS, RequestResult
from scripts.search_transport.executor import RequestExecutor
from scripts.search_transport.transport import get_search_transport


def _percentile(sorted_values, fraction):
    if not sorted_values:
        return None
    index = min(len(sorted_values) - 1, int(len(sorted_values) * fraction))
    return round(sorted_values[index], 1)


def aggregate_benchmark_results(mode: str, results: list[RequestResult], latencies_ms: list[float]) -> dict:
    """Pure aggregation step, deliberately separate from run_one_mode()'s
    Playwright loop so it is unit-testable without a real browser.

    requests/success_count/failure_count/timeout_count are all counted
    from the actual per-request results list -- NOT from the number of
    distinct categories observed (the bug this function replaces: with
    only 4 possible categories, that number was always <= 4 regardless of
    how many requests actually timed out). timeout_count counts only
    requests whose classifier reason_code is literally "timeout" (see
    scripts/search_transport/classifier.py::classify_exception) -- a
    strict subset of failure_count, since RATE_LIMIT/AUTH_CHALLENGE and
    other NETWORK_FAILURE reasons (dns_failure, connection_reset,
    proxy_unavailable, a non-2xx/3xx/429/403 status) are real failures but
    not timeouts."""
    request_count = len(results)
    success_count = sum(1 for result in results if result.category == CATEGORY_SUCCESS)
    failure_count = request_count - success_count
    timeout_count = sum(1 for result in results if result.reason_code == "timeout")

    category_counts = {}
    for result in results:
        category_counts[result.category] = category_counts.get(result.category, 0) + 1

    sorted_latencies = sorted(latencies_ms)

    return {
        "mode": mode,
        "requests": request_count,
        "success_count": success_count,
        "failure_count": failure_count,
        "timeout_count": timeout_count,
        "success_rate": round(success_count / request_count, 3) if request_count else 0.0,
        "timeout_rate": round(timeout_count / request_count, 3) if request_count else 0.0,
        "failure_category_counts": category_counts,
        "p50_latency_ms": _percentile(sorted_latencies, 0.50),
        "p95_latency_ms": _percentile(sorted_latencies, 0.95),
        "mean_latency_ms": round(statistics.mean(latencies_ms), 1) if latencies_ms else None,
    }


def run_one_mode(mode: str, target_url: str, request_count: int, headless: bool) -> dict:
    transport = get_search_transport(mode=mode)
    executor = RequestExecutor(transport)

    latencies_ms = []
    results = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=headless,
            proxy=transport.playwright_proxy_config,
        )
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(transport.read_timeout_ms)
        page.set_default_navigation_timeout(transport.connect_timeout_ms)

        for _ in range(request_count):
            started_at = time.monotonic()
            result = executor.navigate(page, target_url)
            latencies_ms.append((time.monotonic() - started_at) * 1000)
            results.append(result)

        context.close()
        browser.close()

    return aggregate_benchmark_results(mode, results, latencies_ms)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modes", default="direct", help="Comma-separated: direct,proxy")
    parser.add_argument("--requests", type=int, default=5)
    parser.add_argument("--target-url", default=None, help="Defaults to TOR_IP_CHECK_URL (safe, non-LinkedIn)")
    parser.add_argument("--headed", action="store_true", help="Run with a visible browser window")
    args = parser.parse_args()

    target_url = args.target_url or get_tor_ip_check_url()
    modes = [mode.strip() for mode in args.modes.split(",") if mode.strip()]

    print(f"Benchmarking search transport against: {target_url}")
    print(f"Requests per mode: {args.requests}")
    print(f"Modes: {modes}")
    print("-" * 70)

    results = []

    for mode in modes:
        print(f"\nRunning mode={mode} ...")

        try:
            result = run_one_mode(mode, target_url, args.requests, headless=not args.headed)
        except Exception as error:
            print(f"Mode {mode} failed to run: {error}")
            continue

        results.append(result)

        print(
            f"mode={result['mode']} "
            f"requests={result['requests']} "
            f"success_count={result['success_count']} "
            f"failure_count={result['failure_count']} "
            f"timeout_count={result['timeout_count']} "
            f"success_rate={result['success_rate']} "
            f"timeout_rate={result['timeout_rate']} "
            f"p50_latency_ms={result['p50_latency_ms']} "
            f"p95_latency_ms={result['p95_latency_ms']} "
            f"mean_latency_ms={result['mean_latency_ms']} "
            f"failures={result['failure_category_counts']}"
        )

    print("\n" + "=" * 70)
    print("Summary (do not assume proxy is better or worse -- compare the numbers above):")
    for result in results:
        print(f"  {result['mode']}: {result}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
