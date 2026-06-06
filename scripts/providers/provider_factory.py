from pathlib import Path

from app.config import get_job_provider_name
from scripts.providers.json_provider import JsonJobProvider
from scripts.providers.linkedin_provider_placeholder import LinkedInProviderPlaceholder
from scripts.providers.linkedin_browser_provider import LinkedInBrowserProvider


BASE_DIR = Path(__file__).resolve().parent.parent.parent
NEW_JOBS_FILE = BASE_DIR / "sample_data" / "new_jobs.json"


def get_job_provider():
    provider_name = get_job_provider_name()

    if provider_name == "json":
        return JsonJobProvider(NEW_JOBS_FILE)

    if provider_name == "linkedin":
        return LinkedInProviderPlaceholder()

    if provider_name == "linkedin_browser":
        return LinkedInBrowserProvider()

    raise ValueError(f"Unknown job provider: {provider_name}")