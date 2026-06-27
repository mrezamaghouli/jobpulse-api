import argparse
import csv
import subprocess
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--targets", required=True)
    parser.add_argument("--sleep", type=float, default=8.0)
    parser.add_argument("--skip-detail", action="store_true")
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    targets_path = Path(args.targets)
    if not targets_path.exists():
        raise SystemExit("targets file not found: " + str(targets_path))

    with targets_path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    print("loaded targets:", len(rows))

    ok = 0
    failed = 0

    for i, row in enumerate(rows, start=1):
        keywords = (row.get("keywords") or "").strip()
        location = (row.get("location") or "").strip()
        limit = (row.get("limit") or "20").strip()

        if not keywords or not location:
            print("skipped empty row:", row)
            continue

        cmd = [
            sys.executable,
            "scripts/local_linkedin_export.py",
            "--keywords", keywords,
            "--location", location,
            "--limit", limit,
        ]

        if args.skip_detail:
            cmd.append("--skip-detail")

        if args.headless:
            cmd.append("--headless")

        print("")
        print("=" * 100)
        print("TARGET", str(i) + "/" + str(len(rows)), "|", keywords, "|", location, "| limit=" + limit)
        print("CMD:", " ".join(cmd))
        print("=" * 100)

        result = subprocess.run(cmd)

        if result.returncode == 0:
            ok += 1
            print("OK:", keywords, "|", location)
        else:
            failed += 1
            print("FAILED:", keywords, "|", location, "| code=" + str(result.returncode))

        if i < len(rows):
            print("sleeping", args.sleep, "seconds...")
            time.sleep(args.sleep)

    print("")
    print("Batch finished")
    print("ok:", ok)
    print("failed:", failed)


if __name__ == "__main__":
    main()
