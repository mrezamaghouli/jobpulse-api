import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
PROGRESS_FILE = BASE_DIR / "logs" / "linkedin_auto_progress.json"
QUERIES_FILE = BASE_DIR / "config" / "linkedin_expanded_queries.json"


def load_queries_count() -> int:
    if not QUERIES_FILE.exists():
        raise FileNotFoundError(
            f"Expanded queries file not found: {QUERIES_FILE}. "
            "Run: python -m scripts.linkedin_query_expander"
        )

    with QUERIES_FILE.open("r", encoding="utf-8") as file:
        queries = json.load(file)

    return len(queries)


def load_progress() -> dict:
    if not PROGRESS_FILE.exists():
        return {
            "next_offset": 0,
            "runs": []
        }

    with PROGRESS_FILE.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_progress(progress: dict):
    PROGRESS_FILE.parent.mkdir(exist_ok=True)

    with PROGRESS_FILE.open("w", encoding="utf-8") as file:
        json.dump(progress, file, ensure_ascii=False, indent=2)


def run_batch(offset: int, batch_size: int, workers: int, category: str | None):
    command = [
        sys.executable,
        "-m",
        "scripts.linkedin_plan_collect",
        "--max-queries",
        str(batch_size),
        "--offset",
        str(offset),
        "--workers",
        str(workers),
    ]

    if category:
        command.extend(["--category", category])

    print("\n" + "=" * 90)
    print("Running LinkedIn auto-progress batch")
    print(f"Offset: {offset}")
    print(f"Batch size: {batch_size}")
    print(f"Workers: {workers}")
    print(f"Category: {category or 'all'}")
    print("Command:")
    print(" ".join(command))
    print("=" * 90)

    started_at = datetime.now()

    process = subprocess.run(
        command,
        cwd=str(BASE_DIR),
        text=True,
    )

    finished_at = datetime.now()
    duration_seconds = round((finished_at - started_at).total_seconds(), 2)

    return {
        "success": process.returncode == 0,
        "returncode": process.returncode,
        "offset": offset,
        "batch_size": batch_size,
        "workers": workers,
        "category": category,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": duration_seconds,
    }


def run_logo_sync() -> dict:
    print("\n" + "=" * 90)
    print("Running logo sync after batch")
    print("=" * 90)

    started_at = datetime.now()

    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.sync_job_company_logos",
        ],
        cwd=str(BASE_DIR),
        text=True,
        capture_output=True,
    )

    finished_at = datetime.now()
    duration_seconds = round((finished_at - started_at).total_seconds(), 2)

    print(process.stdout)

    if process.stderr:
        print(process.stderr)

    return {
        "success": process.returncode == 0,
        "returncode": process.returncode,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": duration_seconds,
        "stdout": process.stdout,
        "stderr": process.stderr,
    }

def main():
    parser = argparse.ArgumentParser(
        description="Run LinkedIn plan collector with automatic offset progress."
    )

    parser.add_argument(
        "--batch-size",
        type=int,
        default=20,
        help="Number of queries to run in this batch."
    )

    parser.add_argument(
        "--workers",
        type=int,
        default=2,
        help="Number of parallel workers."
    )

    parser.add_argument(
        "--category",
        type=str,
        default=None,
        help="Optional category filter, e.g. data, software, design."
    )

    parser.add_argument(
        "--reset",
        action="store_true",
        help="Reset progress back to offset 0."
    )

    parser.add_argument(
        "--show",
        action="store_true",
        help="Show current progress and exit."
    )

    args = parser.parse_args()

    total_queries = load_queries_count()
    progress = load_progress()

    if args.reset:
        progress = {
            "next_offset": 0,
            "runs": []
        }
        save_progress(progress)
        print("Progress reset to offset 0.")
        return

    if args.show:
        print("LinkedIn auto-progress status")
        print(f"Total expanded queries: {total_queries}")
        print(f"Next offset: {progress.get('next_offset', 0)}")
        print(f"Runs recorded: {len(progress.get('runs', []))}")
        return

    batch_size = max(1, args.batch_size)
    workers = max(1, min(args.workers, 4))

    offset = int(progress.get("next_offset", 0))

    if offset >= total_queries:
        print("All expanded queries have already been processed.")
        print(f"Total queries: {total_queries}")
        print(f"Current offset: {offset}")
        return

    remaining_queries = total_queries - offset
    effective_batch_size = min(batch_size, remaining_queries)

    result = run_batch(
        offset=offset,
        batch_size=effective_batch_size,
        workers=workers,
        category=args.category,
    )

    logo_sync_result = None

    if result["success"]:
        logo_sync_result = run_logo_sync()

    result["logo_sync"] = logo_sync_result

    progress.setdefault("runs", [])
    progress["runs"].append(result)

    if result["success"]:
        progress["next_offset"] = offset + effective_batch_size
        print("\nBatch finished successfully.")
        print(f"Next offset saved: {progress['next_offset']}")

        if logo_sync_result and logo_sync_result["success"]:
            print("Logo sync completed successfully.")
        else:
            print("Batch succeeded, but logo sync failed or did not run.")
    else:
        progress["next_offset"] = offset
        print("\nBatch failed.")
        print(f"Offset kept at: {offset}")

    save_progress(progress)

    print(f"Progress file: {PROGRESS_FILE}")


if __name__ == "__main__":
    main()