import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
STATE_FILE = BASE_DIR / "logs" / "linkedin_adaptive_collect_state.json"
RUN_LOG_DIR = BASE_DIR / "logs" / "adaptive_runs"


PROFILES = [
    {
        "level": 1,
        "name": "safe",
        "batch_size": 5,
        "workers": 1,
        "linkedin_limit": 15,
        "max_pages": 2,
        "company_enrich_limit": 2,
        "max_duration_seconds": 900,
        "max_failed_queries": 1,
    },
    {
        "level": 2,
        "name": "balanced",
        "batch_size": 10,
        "workers": 2,
        "linkedin_limit": 20,
        "max_pages": 3,
        "company_enrich_limit": 3,
        "max_duration_seconds": 1500,
        "max_failed_queries": 2,
    },
    {
        "level": 3,
        "name": "strong",
        "batch_size": 20,
        "workers": 2,
        "linkedin_limit": 25,
        "max_pages": 4,
        "company_enrich_limit": 4,
        "max_duration_seconds": 2400,
        "max_failed_queries": 3,
    },
    {
        "level": 4,
        "name": "aggressive",
        "batch_size": 25,
        "workers": 3,
        "linkedin_limit": 30,
        "max_pages": 4,
        "company_enrich_limit": 5,
        "max_duration_seconds": 3300,
        "max_failed_queries": 4,
    },
    {
        "level": 5,
        "name": "max",
        "batch_size": 35,
        "workers": 3,
        "linkedin_limit": 35,
        "max_pages": 5,
        "company_enrich_limit": 6,
        "max_duration_seconds": 4500,
        "max_failed_queries": 5,
    },
]


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {
            "current_level": 2,
            "stable_successes": 0,
            "runs": [],
        }

    with STATE_FILE.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_state(state: dict):
    STATE_FILE.parent.mkdir(exist_ok=True)

    with STATE_FILE.open("w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)


def get_profile_by_level(level: int) -> dict:
    for profile in PROFILES:
        if profile["level"] == level:
            return profile

    return PROFILES[0]


def clamp_level(level: int) -> int:
    min_level = PROFILES[0]["level"]
    max_level = PROFILES[-1]["level"]

    if level < min_level:
        return min_level

    if level > max_level:
        return max_level

    return level


def extract_failed_queries(output: str) -> int:
    patterns = [
        r"Failed queries:\s*(\d+)",
        r"failed_queries['\"]?\s*:\s*(\d+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, output, re.IGNORECASE)

        if match:
            return int(match.group(1))

    return 0


def output_has_danger_signals(output: str) -> bool:
    danger_keywords = [
        "Internal Server Error",
        "Traceback",
        "TimeoutError",
        "Target page, context or browser has been closed",
        "net::ERR",
        "Connection refused",
        "could not serialize access",
        "deadlock detected",
        "database is locked",
        "out of memory",
        "ENOMEM",
        "Chrome failed",
        "browser has been closed",
    ]

    lower_output = output.lower()

    return any(keyword.lower() in lower_output for keyword in danger_keywords)


def build_env(profile: dict) -> dict:
    env = os.environ.copy()

    env["LINKEDIN_SKIP_EXISTING_ENRICHED"] = env.get(
        "LINKEDIN_SKIP_EXISTING_ENRICHED",
        "true",
    )

    env["LINKEDIN_QUERY_RETRY_COUNT"] = env.get(
        "LINKEDIN_QUERY_RETRY_COUNT",
        "1",
    )

    env["LINKEDIN_QUERY_RETRY_DELAY_SECONDS"] = env.get(
        "LINKEDIN_QUERY_RETRY_DELAY_SECONDS",
        "10",
    )

    env["LINKEDIN_LIMIT"] = str(profile["linkedin_limit"])
    env["LINKEDIN_MAX_PAGES"] = str(profile["max_pages"])
    env["LINKEDIN_STOP_AFTER_NO_NEW_SCROLLS"] = env.get(
        "LINKEDIN_STOP_AFTER_NO_NEW_SCROLLS",
        "2",
    )

    return env


def run_profile(profile: dict, stale_job_days: int, skip_company_enrichment: bool) -> dict:
    command = [
        sys.executable,
        "-m",
        "scripts.linkedin_auto_progress_collect",
        "--batch-size",
        str(profile["batch_size"]),
        "--workers",
        str(profile["workers"]),
        "--company-enrich-limit",
        str(profile["company_enrich_limit"]),
        "--stale-job-days",
        str(stale_job_days),
    ]

    if skip_company_enrichment:
        command.append("--skip-company-enrichment")

    env = build_env(profile)

    RUN_LOG_DIR.mkdir(parents=True, exist_ok=True)

    started_at = datetime.now()

    print("\n" + "=" * 100)
    print("Adaptive LinkedIn collector")
    print(f"Level: {profile['level']} - {profile['name']}")
    print(f"Batch size: {profile['batch_size']}")
    print(f"Workers: {profile['workers']}")
    print(f"LinkedIn limit/query: {profile['linkedin_limit']}")
    print(f"LinkedIn max pages/query: {profile['max_pages']}")
    print(f"Company enrich limit: {profile['company_enrich_limit']}")
    print("Command:")
    print(" ".join(command))
    print("=" * 100)

    process = subprocess.run(
        command,
        cwd=str(BASE_DIR),
        env=env,
        text=True,
        capture_output=True,
    )

    finished_at = datetime.now()
    duration_seconds = round((finished_at - started_at).total_seconds(), 2)

    combined_output = (process.stdout or "") + "\n" + (process.stderr or "")

    failed_queries = extract_failed_queries(combined_output)
    has_danger = output_has_danger_signals(combined_output)
    too_slow = duration_seconds > profile["max_duration_seconds"]
    too_many_failures = failed_queries > profile["max_failed_queries"]

    success = (
        process.returncode == 0
        and not has_danger
        and not too_slow
        and not too_many_failures
    )

    timestamp = started_at.strftime("%Y%m%d_%H%M%S")
    log_file = RUN_LOG_DIR / f"adaptive_level_{profile['level']}_{timestamp}.log"

    with log_file.open("w", encoding="utf-8") as file:
        file.write(combined_output)

    print(process.stdout)

    if process.stderr:
        print(process.stderr)

    print("\n" + "-" * 100)
    print("Adaptive run result")
    print(f"Success: {success}")
    print(f"Return code: {process.returncode}")
    print(f"Duration seconds: {duration_seconds}")
    print(f"Failed queries detected: {failed_queries}")
    print(f"Danger signals detected: {has_danger}")
    print(f"Too slow: {too_slow}")
    print(f"Too many failures: {too_many_failures}")
    print(f"Log file: {log_file}")
    print("-" * 100)

    return {
        "success": success,
        "returncode": process.returncode,
        "profile": profile,
        "duration_seconds": duration_seconds,
        "failed_queries": failed_queries,
        "has_danger": has_danger,
        "too_slow": too_slow,
        "too_many_failures": too_many_failures,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "log_file": str(log_file),
    }


def update_state_after_run(state: dict, result: dict, min_level: int, max_level: int) -> dict:
    current_level = int(state.get("current_level", 2))
    stable_successes = int(state.get("stable_successes", 0))

    if result["success"]:
        stable_successes += 1

        if stable_successes >= 2 and current_level < max_level:
            current_level += 1
            stable_successes = 0
            print(f"System stable. Increasing level to {current_level}.")
        else:
            print(f"System stable at level {current_level}. Stable successes: {stable_successes}")

    else:
        current_level -= 1
        stable_successes = 0
        print(f"System overloaded or unstable. Backing off to level {current_level}.")

    current_level = max(min_level, min(current_level, max_level))

    state["current_level"] = current_level
    state["stable_successes"] = stable_successes
    state.setdefault("runs", [])
    state["runs"].append(result)

    if len(state["runs"]) > 100:
        state["runs"] = state["runs"][-100:]

    return state


def main():
    parser = argparse.ArgumentParser(
        description="Adaptive LinkedIn collector that auto-tunes batch size, workers, limits and pages."
    )

    parser.add_argument(
        "--cycles",
        type=int,
        default=1,
        help="How many adaptive batches to run.",
    )

    parser.add_argument(
        "--min-level",
        type=int,
        default=1,
        help="Minimum adaptive level.",
    )

    parser.add_argument(
        "--max-level",
        type=int,
        default=5,
        help="Maximum adaptive level.",
    )

    parser.add_argument(
        "--start-level",
        type=int,
        default=None,
        help="Force start level for this run.",
    )

    parser.add_argument(
        "--stale-job-days",
        type=int,
        default=30,
        help="Mark jobs inactive if not seen for this many days.",
    )

    parser.add_argument(
        "--skip-company-enrichment",
        action="store_true",
        help="Skip company enrichment for faster collection.",
    )

    parser.add_argument(
        "--show",
        action="store_true",
        help="Show adaptive state and exit.",
    )

    parser.add_argument(
        "--reset",
        action="store_true",
        help="Reset adaptive state.",
    )

    args = parser.parse_args()

    min_level = clamp_level(args.min_level)
    max_level = clamp_level(args.max_level)

    if min_level > max_level:
        min_level, max_level = max_level, min_level

    state = load_state()

    if args.reset:
        state = {
            "current_level": args.start_level or 2,
            "stable_successes": 0,
            "runs": [],
        }
        state["current_level"] = max(min_level, min(clamp_level(state["current_level"]), max_level))
        save_state(state)
        print("Adaptive state reset.")
        print(f"Current level: {state['current_level']}")
        return

    if args.show:
        print("LinkedIn adaptive collector state")
        print(f"Current level: {state.get('current_level')}")
        print(f"Stable successes: {state.get('stable_successes')}")
        print(f"Recorded runs: {len(state.get('runs', []))}")
        print(f"State file: {STATE_FILE}")
        return

    if args.start_level is not None:
        state["current_level"] = max(min_level, min(clamp_level(args.start_level), max_level))
        state["stable_successes"] = 0

    cycles = max(1, min(args.cycles, 20))

    for cycle_index in range(1, cycles + 1):
        current_level = max(min_level, min(clamp_level(int(state.get("current_level", 2))), max_level))
        profile = get_profile_by_level(current_level)

        print(f"\nAdaptive cycle {cycle_index}/{cycles}")

        result = run_profile(
            profile=profile,
            stale_job_days=max(1, args.stale_job_days),
            skip_company_enrichment=args.skip_company_enrichment,
        )

        state = update_state_after_run(
            state=state,
            result=result,
            min_level=min_level,
            max_level=max_level,
        )

        save_state(state)

    print("\nAdaptive collection finished.")
    print(f"Final level for next run: {state.get('current_level')}")
    print(f"Stable successes: {state.get('stable_successes')}")
    print(f"State file: {STATE_FILE}")


if __name__ == "__main__":
    main()