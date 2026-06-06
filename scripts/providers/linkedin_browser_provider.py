import json
import re
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

from playwright.sync_api import sync_playwright

from app.config import (
    get_linkedin_browser,
    get_linkedin_keywords,
    get_linkedin_location,
    get_linkedin_limit,
)


BASE_DIR = Path(__file__).resolve().parent.parent.parent
AUTH_FILE = BASE_DIR / ".auth" / "linkedin_storage_state.json"
OUTPUT_DIR = BASE_DIR / "sample_output"
OUTPUT_FILE = OUTPUT_DIR / "linkedin_browser_provider_last_run.json"


class LinkedInBrowserProvider:
    def __init__(self):
        self.keywords = get_linkedin_keywords()
        self.location = get_linkedin_location()
        self.limit = get_linkedin_limit()
        self.browser_channel = get_linkedin_browser()

    def fetch_jobs(self):
        if not AUTH_FILE.exists():
            raise FileNotFoundError(
                f"LinkedIn auth session not found: {AUTH_FILE}. "
                "Run scripts/linkedin_save_session_from_browser.py first."
            )

        OUTPUT_DIR.mkdir(exist_ok=True)

        search_url = self.build_jobs_search_url(
            keywords=self.keywords,
            location=self.location
        )

        print("LinkedInBrowserProvider started.")
        print(f"Keywords: {self.keywords}")
        print(f"Location: {self.location}")
        print(f"Limit: {self.limit}")
        print(f"Search URL: {search_url}")

        with sync_playwright() as playwright:
            browser = self.launch_browser(playwright)

            context = browser.new_context(
                storage_state=str(AUTH_FILE),
                viewport={
                    "width": 1366,
                    "height": 900
                }
            )

            page = context.new_page()

            print("Opening LinkedIn Jobs search page...")
            page.goto(search_url, wait_until="domcontentloaded", timeout=60000)

            time.sleep(3)

            current_url = page.url
            print(f"Current URL: {current_url}")

            if "/login" in current_url or "checkpoint" in current_url:
                print("\nLinkedIn asked for login/checkpoint.")
                print("Complete it manually in the opened browser window.")
                input("Press ENTER after the jobs page is visible... ")

            raw_jobs = self.extract_jobs_from_page(page, self.limit)

            normalized_jobs = [
                self.normalize_job(raw_job)
                for raw_job in raw_jobs
            ]

            with OUTPUT_FILE.open("w", encoding="utf-8") as file:
                json.dump(normalized_jobs, file, ensure_ascii=False, indent=2)

            print(f"\nExtracted LinkedIn jobs: {len(normalized_jobs)}")
            print(f"Debug output: {OUTPUT_FILE}")

            for index, job in enumerate(normalized_jobs, start=1):
                print(f"{index}. {job['title']} | {job['company']} | {job['location']}")
                print(f"   {job['job_url']}")

            browser.close()

            return normalized_jobs

    def launch_browser(self, playwright):
        supported_channels = {
            "chrome": "chrome",
            "edge": "msedge",
            "msedge": "msedge",
        }

        channel = supported_channels.get(self.browser_channel, "chrome")

        print(f"Launching browser channel: {channel}")

        return playwright.chromium.launch(
            channel=channel,
            headless=False,
            slow_mo=200
        )

    def build_jobs_search_url(self, keywords: str, location: str) -> str:
        query_params = {
            "keywords": keywords,
            "location": location,
            "f_TPR": "r604800",
            "sortBy": "DD",
        }

        return f"https://www.linkedin.com/jobs/search/?{urlencode(query_params)}"

    def extract_jobs_from_page(self, page, limit: int) -> list[dict]:
        page.wait_for_timeout(4000)

        for _ in range(3):
            page.mouse.wheel(0, 1200)
            page.wait_for_timeout(1500)

        jobs = page.evaluate(
            """
            (limit) => {
                const cards = Array.from(document.querySelectorAll(
                    'li[data-occludable-job-id], div.job-card-container, div[data-job-id]'
                ));

                const unique = [];
                const seen = new Set();

                for (const card of cards) {
                    const dataJobId =
                        card.getAttribute('data-occludable-job-id') ||
                        card.getAttribute('data-job-id') ||
                        '';

                    const titleEl =
                        card.querySelector('.job-card-list__title--link') ||
                        card.querySelector('.job-card-list__title') ||
                        card.querySelector('a[href*="/jobs/view/"]') ||
                        card.querySelector('strong');

                    const companyEl =
                        card.querySelector('.job-card-container__company-name') ||
                        card.querySelector('[class*="company-name"]');

                    const locationEl =
                        card.querySelector('.job-card-container__metadata-item') ||
                        card.querySelector('[class*="metadata-item"]');

                    const linkEl =
                        card.querySelector('a[href*="/jobs/view/"]');

                    const title = titleEl ? titleEl.innerText.trim() : '';
                    const company = companyEl ? companyEl.innerText.trim() : '';
                    const location = locationEl ? locationEl.innerText.trim() : '';

                    let jobUrl = linkEl ? linkEl.href : '';

                    if (jobUrl && jobUrl.startsWith('/')) {
                        jobUrl = `https://www.linkedin.com${jobUrl}`;
                    }

                    if (!title || !jobUrl) {
                        continue;
                    }

                    const key = dataJobId || jobUrl;

                    if (seen.has(key)) {
                        continue;
                    }

                    seen.add(key);

                    unique.push({
                        linkedin_job_id: dataJobId,
                        title,
                        company,
                        location,
                        job_url: jobUrl,
                        company_linkedin_url: null
                    });

                    if (unique.length >= limit) {
                        break;
                    }
                }

                return unique;
            }
            """,
            limit
        )

        return jobs

    def normalize_job(self, raw_job: dict) -> dict:
        job_url = raw_job.get("job_url", "")
        linkedin_job_id = self.extract_job_id(
            job_url=job_url,
            fallback=raw_job.get("linkedin_job_id", "")
        )

        return {
            "linkedin_job_id": linkedin_job_id,
            "title": self.clean_text(raw_job.get("title")),
            "company": self.clean_text(raw_job.get("company")),
            "company_linkedin_url": raw_job.get("company_linkedin_url") or None,

            "location": self.clean_text(raw_job.get("location")),
            "remote": "remote" in self.clean_text(raw_job.get("location")).lower(),

            "job_type": None,
            "seniority": None,

            "salary_min": None,
            "salary_max": None,
            "currency": None,

            "source": "LinkedIn",
            "job_url": job_url,

            "poster_name": None,
            "poster_title": None,
            "poster_profile_url": None,

            "date_posted": str(date.today()),
        }

    def extract_job_id(self, job_url: str, fallback: str = "") -> str:
        if fallback:
            return fallback

        match = re.search(r"/jobs/view/(\d+)", job_url)

        if match:
            return match.group(1)

        return ""

    def clean_text(self, value: str | None) -> str:
        if not value:
            return ""

        return re.sub(r"\s+", " ", value).strip()