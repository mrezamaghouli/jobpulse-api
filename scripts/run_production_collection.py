import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent


PROFILES = {
    "safe": {
        "batch_size": 5,
        "workers": 1,
        "linkedin_limit": 15,
        "linkedin_max_pages": 2,
        "cooldown_hours": 12,
        "company_enrich_limit": 2,
    },
    "balanced": {
        "batch_size": 10,
        "workers": 2,
        "linkedin_limit": 20,
        "linkedin_max_pages": 3,
        "cooldown_hours": 12,
        "company_enrich_limit": 3,
    },
    "strong": {
        "batch_size": 15,
        "workers": 2,
        "linkedin_limit": 25,
        "linkedin_max_pages": 4,
        "cooldown_hours": 8,
        "company_enrich_limit": 5,
    },
}


def apply_common_env(profile: dict):
    os.environ.setdefault("POSTGRES_HOST", "127.0.0.1")
    os.environ.setdefault("POSTGRES_PORT", "5432")
    os.environ.setdefault("POSTGRES_DB", "jobpulse")
    os.environ.setdefault("POSTGRES_USER", "jobpulse_user")
    os.environ.setdefault("POSTGRES_PASSWORD", "jobpulse_password")

    os.environ.setdefault("LINKEDIN_BROWSER", "chrome")
    os.environ.setdefault("LINKEDIN_SKIP_EXISTING_ENRICHED", "true")
    os.environ.setdefault("LINKEDIN_QUERY_RETRY_COUNT", "1")
    os.environ.setdefault("LINKEDIN_QUERY_RETRY_DELAY_SECONDS", "10")

    os.environ["LINKEDIN_LIMIT"] = str(profile["linkedin_limit"])
    os.environ["LINKEDIN_MAX_PAGES"] = str(profile["linkedin_max_pages"])
    os.environ["LINKEDIN_QUERY_COOLDOWN_HOURS"] = str(profile["cooldown_hours"])
    os.environ["COMPANY_ENRICH_LIMIT"] = str(profile["company_enrich_limit"])

    os.environ.setdefault("JOB_INACTIVE_AFTER_DAYS", "30")
    os.environ.setdefault("JOB_ARCHIVE_AFTER_DAYS", "60")
    os.environ.setdefault("JOB_HARD_DELETE_ENABLED", "false")


def run_command(command: list[str], title: str) -> int:
    print("")
    print("=" * 100)
    print(title)
    print("=" * 100)
    print(" ".join(command))
    print("")

    process = subprocess.run(
        command,
        cwd=str(BASE_DIR),
        env=os.environ.copy(),
        text=True,
    )

    return process.returncode


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--profile",
        choices=list(PROFILES.keys()),
        default="balanced",
    )
    parser.add_argument(
        "--cycles",
        type=int,
        default=1,
    )
    parser.add_argument(
        "--skip-company-enrichment",
        action="store_true",
    )
    parser.add_argument(
        "--show",
        action="store_true",
    )

    args = parser.parse_args()

    profile = PROFILES[args.profile]
    apply_common_env(profile)

    print("JobPulse Production Collection Runner")
    print(f"Started at: {datetime.now().isoformat()}")
    print(f"Profile: {args.profile}")
    print(f"Cycles: {args.cycles}")
    print(f"Batch size: {profile['batch_size']}")
    print(f"Workers: {profile['workers']}")
    print(f"LinkedIn limit: {profile['linkedin_limit']}")
    print(f"LinkedIn max pages: {profile['linkedin_max_pages']}")
    print(f"Cooldown hours: {profile['cooldown_hours']}")
    print(f"Skip company enrichment: {args.skip_company_enrichment}")

    if args.show:
        return

    for cycle in range(1, args.cycles + 1):
        print("")
        print("#" * 100)
        print(f"Collection cycle {cycle}/{args.cycles}")
        print("#" * 100)

        command = [
            sys.executable,
            "-m",
            "scripts.linkedin_auto_progress_collect",
            "--batch-size",
            str(profile["batch_size"]),
            "--workers",
            str(profile["workers"]),
            "--stale-job-days",
            os.environ.get("JOB_INACTIVE_AFTER_DAYS", "30"),
        ]

        if args.skip_company_enrichment:
            command.append("--skip-company-enrichment")
        else:
            command.extend(
                [
                    "--company-enrich-limit",
                    str(profile["company_enrich_limit"]),
                ]
            )

        returncode = run_command(
            command=command,
            title=f"Running smart production collection cycle {cycle}",
        )

        if returncode != 0:
            print(f"Cycle {cycle} failed with return code: {returncode}")
            sys.exit(returncode)

    print("")
    print("=" * 100)
    print("Production collection finished successfully.")
    print("=" * 100)


if __name__ == "__main__":
    main()