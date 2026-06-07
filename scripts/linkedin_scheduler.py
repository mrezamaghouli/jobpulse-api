import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "linkedin_scheduler.log"


def get_interval_minutes(default_interval: int = 180) -> int:
    raw_value = os.getenv("LINKEDIN_SCHEDULE_INTERVAL_MINUTES", str(default_interval))

    try:
        interval = int(raw_value)
    except ValueError:
        interval = default_interval

    if interval < 1:
        interval = 1

    return interval


def write_log(message: str):
    LOG_DIR.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"

    print(line)

    with LOG_FILE.open("a", encoding="utf-8") as file:
        file.write(line + "\n")


def validate_required_environment():
    required_variables = [
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
    ]

    missing_variables = [
        variable
        for variable in required_variables
        if not os.getenv(variable)
    ]

    if missing_variables:
        raise RuntimeError(
            "Missing required environment variables: "
            + ", ".join(missing_variables)
        )


def run_linkedin_collection() -> bool:
    write_log("Starting scheduled LinkedIn collection...")

    started_at = datetime.now()

    process = subprocess.run(
        [sys.executable, "-m", "scripts.linkedin_multi_collect"],
        cwd=str(BASE_DIR),
        env=os.environ.copy(),
        text=True,
        capture_output=True
    )

    finished_at = datetime.now()
    duration_seconds = round((finished_at - started_at).total_seconds(), 2)

    if process.stdout:
        write_log("Collector stdout:")
        for line in process.stdout.splitlines():
            write_log(f"  {line}")

    if process.stderr:
        write_log("Collector stderr:")
        for line in process.stderr.splitlines():
            write_log(f"  {line}")

    if process.returncode == 0:
        write_log(f"Scheduled LinkedIn collection finished successfully in {duration_seconds}s.")
        return True

    write_log(f"Scheduled LinkedIn collection failed in {duration_seconds}s.")
    return False


def run_scheduler(interval_minutes: int, run_once: bool):
    validate_required_environment()

    write_log("=" * 70)
    write_log("JobPulse LinkedIn scheduler started.")
    write_log(f"Interval: {interval_minutes} minute(s)")
    write_log(f"Run once: {run_once}")
    write_log("=" * 70)

    while True:
        run_linkedin_collection()

        if run_once:
            write_log("Run-once mode finished. Exiting scheduler.")
            break

        sleep_seconds = interval_minutes * 60
        next_run_at = datetime.now().timestamp() + sleep_seconds
        next_run_text = datetime.fromtimestamp(next_run_at).strftime("%Y-%m-%d %H:%M:%S")

        write_log(f"Next scheduled run at: {next_run_text}")

        try:
            time.sleep(sleep_seconds)
        except KeyboardInterrupt:
            write_log("Scheduler stopped by user.")
            break


def main():
    parser = argparse.ArgumentParser(description="Run scheduled LinkedIn job collection.")

    parser.add_argument(
        "--interval-minutes",
        type=int,
        default=None,
        help="How often to run the LinkedIn collector."
    )

    parser.add_argument(
        "--run-once",
        action="store_true",
        help="Run the collector once and exit."
    )

    args = parser.parse_args()

    interval_minutes = args.interval_minutes or get_interval_minutes()

    run_scheduler(
        interval_minutes=interval_minutes,
        run_once=args.run_once
    )


if __name__ == "__main__":
    main()