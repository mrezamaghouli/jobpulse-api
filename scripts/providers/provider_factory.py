import os
from pathlib import Path

from scripts.providers.json_provider import JsonJobProvider
from scripts.providers.linkedin_provider_placeholder import LinkedInProviderPlaceholder


BASE_DIR = Path(__file__).resolve().parent.parent.parent
NEW_JOBS_FILE = BASE_DIR / "sample_data" / "new_jobs.json"


def get_job_provider():
    provider_name = os.getenv("JOB_PROVIDER", "json").lower().strip()

    if provider_name == "json":
        return JsonJobProvider(NEW_JOBS_FILE)

    if provider_name == "linkedin":
        return LinkedInProviderPlaceholder()

    raise ValueError(f"Unknown job provider: {provider_name}")