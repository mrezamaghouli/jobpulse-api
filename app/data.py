import json
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
JOBS_FILE = BASE_DIR / "sample_data" / "jobs.json"


def load_jobs():
    if not JOBS_FILE.exists():
        return []

    with open(JOBS_FILE, "r", encoding="utf-8") as file:
        return json.load(file)