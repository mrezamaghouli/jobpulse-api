import json
import re
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from app.config import (
    get_linkedin_browser,
    get_linkedin_keywords,
    get_linkedin_limit,
    get_linkedin_location,
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
            location=self.location,
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
                    "height": 900,
                },
            )

            page = context.new_page()

            print("Opening LinkedIn Jobs search page...")
            self.open_search_page(page, search_url)

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
                print(
                    f"{index}. {job['title']} | "
                    f"{job['company']} | "
                    f"{job['location']} | "
                    f"{job.get('apply_type', 'unknown')}"
                )
                print(f"   Job: {job['job_url']}")

                if job.get("apply_url"):
                    print(f"   Apply: {job['apply_url']}")

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
            slow_mo=200,
        )

    def build_jobs_search_url(self, keywords: str, location: str) -> str:
        query_params = {
            "keywords": keywords,
            "f_TPR": "r604800",
            "sortBy": "DD",
        }

        if location:
            query_params["location"] = location

        return f"https://www.linkedin.com/jobs/search/?{urlencode(query_params)}"

    def open_search_page(self, page, search_url: str):
        page.set_default_timeout(30000)
        page.set_default_navigation_timeout(120000)

        try:
            page.goto(
                search_url,
                wait_until="commit",
                timeout=120000,
            )

            page.wait_for_timeout(5000)
            return

        except PlaywrightTimeoutError as error:
            print(f"LinkedIn navigation timeout: {error}")
            print("Trying to continue with the current page state...")

            current_url = page.url or ""

            if "linkedin.com" in current_url:
                page.wait_for_timeout(5000)
                return

            print("Retrying LinkedIn search page once...")

            page.goto(
                search_url,
                wait_until="commit",
                timeout=120000,
            )

            page.wait_for_timeout(5000)

    def extract_jobs_from_page(self, page, limit: int) -> list[dict]:
        print("Waiting for LinkedIn job results...")

        try:
            page.wait_for_selector(
                'a[href*="/jobs/view/"], li[data-occludable-job-id], div.job-card-container, div[data-job-id]',
                timeout=30000,
            )
        except Exception as error:
            print(f"Job result selector did not appear in time: {error}")

        page.wait_for_timeout(5000)

        for scroll_index in range(6):
            print(f"Scrolling job results: {scroll_index + 1}/6")
            page.mouse.wheel(0, 1400)
            page.wait_for_timeout(1800)

        debug_counts = page.evaluate(
            """
            () => {
                return {
                    currentUrl: window.location.href,
                    title: document.title,
                    bodyTextStart: document.body ? document.body.innerText.slice(0, 1000) : '',
                    jobViewLinks: document.querySelectorAll('a[href*="/jobs/view/"]').length,
                    occludableCards: document.querySelectorAll('li[data-occludable-job-id]').length,
                    jobCardContainers: document.querySelectorAll('div.job-card-container').length,
                    dataJobIdCards: document.querySelectorAll('div[data-job-id]').length
                };
            }
            """
        )

        print("LinkedIn page debug:")
        print(debug_counts)

        basic_jobs = page.evaluate(
            """
            (limit) => {
                const clean = (value) => {
                    if (!value) {
                        return '';
                    }

                    return value.replace(/\\s+/g, ' ').trim();
                };

                const absoluteUrl = (url) => {
                    if (!url) {
                        return '';
                    }

                    if (url.startsWith('http')) {
                        return url;
                    }

                    if (url.startsWith('/')) {
                        return `https://www.linkedin.com${url}`;
                    }

                    return url;
                };

                const extractJobIdFromUrl = (url) => {
                    if (!url) {
                        return '';
                    }

                    const patterns = [
                        /\\/jobs\\/view\\/(\\d+)/,
                        /currentJobId=(\\d+)/,
                        /jobId=(\\d+)/
                    ];

                    for (const pattern of patterns) {
                        const match = url.match(pattern);

                        if (match) {
                            return match[1];
                        }
                    }

                    return '';
                };

                const getText = (root, selectors) => {
                    for (const selector of selectors) {
                        const el = root.querySelector(selector);

                        if (el && clean(el.innerText)) {
                            return clean(el.innerText);
                        }

                        if (el && clean(el.getAttribute('aria-label'))) {
                            return clean(el.getAttribute('aria-label'));
                        }

                        if (el && clean(el.getAttribute('title'))) {
                            return clean(el.getAttribute('title'));
                        }
                    }

                    return '';
                };

                const getClosestCard = (link) => {
                    return (
                        link.closest('li[data-occludable-job-id]') ||
                        link.closest('div.job-card-container') ||
                        link.closest('div[data-job-id]') ||
                        link.closest('li') ||
                        link.closest('div')
                    );
                };

                const links = Array.from(document.querySelectorAll('a[href*="/jobs/view/"]'));

                const jobs = [];
                const seen = new Set();

                for (const link of links) {
                    let jobUrl = absoluteUrl(link.href || link.getAttribute('href') || '');

                    if (!jobUrl) {
                        continue;
                    }

                    const linkedinJobId = extractJobIdFromUrl(jobUrl);
                    const card = getClosestCard(link) || document;

                    const key = linkedinJobId || jobUrl.split('?')[0];

                    if (seen.has(key)) {
                        continue;
                    }

                    seen.add(key);

                    let title =
                        clean(link.innerText) ||
                        clean(link.getAttribute('aria-label')) ||
                        clean(link.getAttribute('title')) ||
                        getText(card, [
                            '.job-card-list__title--link',
                            '.job-card-list__title',
                            '[class*="job-title"]',
                            'strong',
                            'h3'
                        ]);

                    title = title
                        .replace(/^View job\\s*/i, '')
                        .replace(/^Open job\\s*/i, '')
                        .trim();

                    const company = getText(card, [
                        '.job-card-container__company-name',
                        '[class*="company-name"]',
                        '.artdeco-entity-lockup__subtitle',
                        '[class*="subtitle"]'
                    ]);

                    const location = getText(card, [
                        '.job-card-container__metadata-item',
                        '[class*="metadata-item"]',
                        '.artdeco-entity-lockup__caption',
                        '[class*="caption"]'
                    ]);

                    if (!title) {
                        continue;
                    }

                    jobs.push({
                        linkedin_job_id: linkedinJobId,
                        title,
                        company,
                        location,
                        job_url: jobUrl,
                        company_linkedin_url: null,
                        apply_type: 'unknown',
                        apply_url: null,
                        apply_label: null
                    });

                    if (jobs.length >= limit) {
                        break;
                    }
                }

                return jobs;
            }
            """,
            limit,
        )

        if not basic_jobs:
            debug_file = OUTPUT_DIR / "linkedin_debug_no_jobs.json"

            debug_data = {
                "url": page.url,
                "counts": debug_counts,
            }

            with debug_file.open("w", encoding="utf-8") as file:
                json.dump(debug_data, file, ensure_ascii=False, indent=2)

            try:
                screenshot_file = OUTPUT_DIR / "linkedin_debug_no_jobs.png"
                page.screenshot(path=str(screenshot_file), full_page=True)
                print(f"Debug screenshot saved: {screenshot_file}")
            except Exception as error:
                print(f"Could not save debug screenshot: {error}")

            print(f"Debug file saved: {debug_file}")
            return []

        print(f"Basic LinkedIn jobs found before enrichment: {len(basic_jobs)}")

        enriched_jobs = []

        for index, job in enumerate(basic_jobs, start=1):
            print(f"Reading detail panel for job {index}/{len(basic_jobs)}...")

            detail_data = self.extract_detail_for_job(page, job)

            merged_job = {
                **job,
                **detail_data,
            }

            enriched_jobs.append(merged_job)

            page.wait_for_timeout(1200)

        return enriched_jobs

    def extract_detail_for_job(self, page, job: dict) -> dict:
        job_id = self.extract_job_id(
            job_url=job.get("job_url", ""),
            fallback=job.get("linkedin_job_id", ""),
        )

        if job_id:
            selector = (
                f'li[data-occludable-job-id="{job_id}"], '
                f'div[data-job-id="{job_id}"]'
            )
        else:
            selector = f'a[href="{job.get("job_url", "")}"]'

        try:
            card = page.locator(selector).first

            if card.count() > 0:
                card.click(timeout=8000)
                page.wait_for_timeout(2500)

        except Exception as error:
            print(f"Could not click job card: {error}")

        detail_data = page.evaluate(
            """
            () => {
                const getText = (selectors) => {
                    for (const selector of selectors) {
                        const el = document.querySelector(selector);

                        if (el && el.innerText && el.innerText.trim()) {
                            return el.innerText.trim();
                        }
                    }

                    return '';
                };

                const getHref = (selectors) => {
                    for (const selector of selectors) {
                        const el = document.querySelector(selector);

                        if (el && el.href) {
                            return el.href;
                        }

                        if (el && el.getAttribute && el.getAttribute('href')) {
                            return el.getAttribute('href');
                        }
                    }

                    return '';
                };

                const title = getText([
                    '.jobs-unified-top-card__job-title',
                    '.job-details-jobs-unified-top-card__job-title',
                    'h1'
                ]);

                const company = getText([
                    '.jobs-unified-top-card__company-name',
                    '.job-details-jobs-unified-top-card__company-name',
                    '.jobs-unified-top-card__primary-description-container a',
                    'a[href*="/company/"]'
                ]);

                const location = getText([
                    '.jobs-unified-top-card__bullet',
                    '.job-details-jobs-unified-top-card__primary-description-container span',
                    '.jobs-unified-top-card__primary-description-container span'
                ]);

                const companyUrl = getHref([
                    '.jobs-unified-top-card__company-name a',
                    '.job-details-jobs-unified-top-card__company-name a',
                    'a[href*="/company/"]'
                ]);

                const detailsText = getText([
                    '.jobs-unified-top-card__primary-description-container',
                    '.job-details-jobs-unified-top-card__primary-description-container'
                ]);

                return {
                    detail_title: title,
                    detail_company: company,
                    detail_location: location,
                    company_linkedin_url: companyUrl || null,
                    details_text: detailsText
                };
            }
            """
        )

        apply_info = self.extract_apply_info_from_detail_page(
            page=page,
            job_url=job.get("job_url", ""),
        )

        return {
            "title": detail_data.get("detail_title") or job.get("title", ""),
            "company": detail_data.get("detail_company") or job.get("company", ""),
            "location": self.extract_location_from_details(
                detail_location=detail_data.get("detail_location", ""),
                details_text=detail_data.get("details_text", ""),
                fallback=job.get("location", ""),
            ),
            "company_linkedin_url": (
                detail_data.get("company_linkedin_url")
                or job.get("company_linkedin_url")
            ),
            "apply_type": apply_info.get("apply_type") or "unknown",
            "apply_url": apply_info.get("apply_url"),
            "apply_label": apply_info.get("apply_label"),
        }

    def extract_apply_info_from_detail_page(self, page, job_url: str) -> dict:
        basic_apply_info = page.evaluate(
            """
            () => {
                const isVisible = (el) => {
                    if (!el) {
                        return false;
                    }

                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();

                    return (
                        style &&
                        style.visibility !== 'hidden' &&
                        style.display !== 'none' &&
                        rect.width > 0 &&
                        rect.height > 0
                    );
                };

                const elements = Array.from(document.querySelectorAll('button, a'));

                const candidates = elements
                    .filter(isVisible)
                    .map((el, index) => {
                        const label = (
                            el.innerText ||
                            el.getAttribute('aria-label') ||
                            el.getAttribute('title') ||
                            ''
                        ).trim();

                        const labelLower = label.toLowerCase();
                        const href = el.href || el.getAttribute('href') || '';

                        return {
                            index,
                            tagName: el.tagName.toLowerCase(),
                            label,
                            labelLower,
                            href,
                            className: el.className || '',
                            ariaLabel: el.getAttribute('aria-label') || ''
                        };
                    })
                    .filter((item) => {
                        return (
                            item.labelLower.includes('easy apply') ||
                            item.labelLower === 'apply' ||
                            item.labelLower.includes('apply now') ||
                            item.labelLower.includes('apply to') ||
                            item.labelLower.includes('bewerben')
                        );
                    });

                for (const item of candidates) {
                    if (item.labelLower.includes('easy apply')) {
                        return {
                            found: true,
                            apply_type: 'easy_apply',
                            apply_url: window.location.href,
                            apply_label: item.label || 'Easy Apply',
                            candidate_index: item.index,
                            needs_click: false
                        };
                    }
                }

                for (const item of candidates) {
                    if (
                        item.labelLower === 'apply' ||
                        item.labelLower.includes('apply now') ||
                        item.labelLower.includes('apply to') ||
                        item.labelLower.includes('bewerben')
                    ) {
                        if (item.href) {
                            return {
                                found: true,
                                apply_type: 'external',
                                apply_url: item.href,
                                apply_label: item.label || 'Apply',
                                candidate_index: item.index,
                                needs_click: false
                            };
                        }

                        return {
                            found: true,
                            apply_type: 'external',
                            apply_url: null,
                            apply_label: item.label || 'Apply',
                            candidate_index: item.index,
                            needs_click: true
                        };
                    }
                }

                return {
                    found: false,
                    apply_type: 'unknown',
                    apply_url: null,
                    apply_label: null,
                    candidate_index: null,
                    needs_click: false
                };
            }
            """
        )

        apply_type = basic_apply_info.get("apply_type") or "unknown"
        apply_url = basic_apply_info.get("apply_url")
        apply_label = basic_apply_info.get("apply_label")

        if apply_type == "easy_apply":
            return {
                "apply_type": "easy_apply",
                "apply_url": job_url,
                "apply_label": apply_label or "Easy Apply",
            }

        if apply_type == "external" and apply_url:
            return {
                "apply_type": "external",
                "apply_url": self.absolute_url(apply_url),
                "apply_label": apply_label or "Apply",
            }

        if apply_type == "external" and basic_apply_info.get("needs_click"):
            clicked_apply_url = self.try_capture_external_apply_url(page)

            if clicked_apply_url:
                return {
                    "apply_type": "external",
                    "apply_url": clicked_apply_url,
                    "apply_label": apply_label or "Apply",
                }

            return {
                "apply_type": "external",
                "apply_url": None,
                "apply_label": apply_label or "Apply",
            }

        return {
            "apply_type": "unknown",
            "apply_url": None,
            "apply_label": None,
        }

    def try_capture_external_apply_url(self, page) -> str | None:
        original_url = page.url

        selectors = [
            'button:has-text("Apply")',
            'button:has-text("Apply now")',
            'button:has-text("Bewerben")',
            'a:has-text("Apply")',
            'a:has-text("Apply now")',
            'a:has-text("Bewerben")',
            '.jobs-apply-button',
            '.jobs-s-apply button',
        ]

        for selector in selectors:
            try:
                locator = page.locator(selector).first

                if locator.count() == 0:
                    continue

                if not locator.is_visible(timeout=2000):
                    continue

                try:
                    with page.context.expect_page(timeout=6000) as popup_info:
                        locator.click(timeout=5000)

                    popup = popup_info.value
                    popup.wait_for_load_state("domcontentloaded", timeout=10000)

                    external_url = popup.url

                    popup.close()

                    if external_url and "linkedin.com" not in external_url:
                        return external_url

                except Exception:
                    pass

                try:
                    locator.click(timeout=5000)
                    page.wait_for_timeout(5000)

                    current_url = page.url

                    if (
                        current_url
                        and current_url != original_url
                        and "linkedin.com" not in current_url
                    ):
                        external_url = current_url

                        page.goto(
                            original_url,
                            wait_until="commit",
                            timeout=30000,
                        )
                        page.wait_for_timeout(3000)

                        return external_url

                    if current_url != original_url:
                        page.goto(
                            original_url,
                            wait_until="commit",
                            timeout=30000,
                        )
                        page.wait_for_timeout(3000)

                except Exception:
                    try:
                        if page.url != original_url:
                            page.goto(
                                original_url,
                                wait_until="commit",
                                timeout=30000,
                            )
                            page.wait_for_timeout(3000)
                    except Exception:
                        pass

            except Exception:
                continue

        return None

    def extract_location_from_details(
        self,
        detail_location: str,
        details_text: str,
        fallback: str,
    ) -> str:
        detail_location = self.clean_text(detail_location)
        details_text = self.clean_text(details_text)
        fallback = self.clean_text(fallback)

        if detail_location:
            return detail_location

        if details_text:
            parts = [part.strip() for part in details_text.split("·")]

            for part in parts:
                lower_part = part.lower()

                if any(
                    keyword in lower_part
                    for keyword in [
                        "germany",
                        "berlin",
                        "munich",
                        "hamburg",
                        "frankfurt",
                        "remote",
                    ]
                ):
                    return part

        return fallback

    def normalize_job(self, raw_job: dict) -> dict:
        raw_job_url = raw_job.get("job_url", "")

        linkedin_job_id = self.extract_job_id(
            job_url=raw_job_url,
            fallback=raw_job.get("linkedin_job_id", ""),
        )

        job_url = self.canonicalize_job_url(
            job_url=raw_job_url,
            linkedin_job_id=linkedin_job_id,
        )

        title = self.clean_text(raw_job.get("title"))
        company = self.clean_text(raw_job.get("company")) or "Unknown Company"
        location = self.clean_text(raw_job.get("location")) or "Unknown Location"

        apply_type = raw_job.get("apply_type") or "unknown"
        apply_url = raw_job.get("apply_url")
        apply_label = raw_job.get("apply_label")

        if apply_type == "easy_apply" and not apply_url:
            apply_url = job_url

        return {
            "linkedin_job_id": linkedin_job_id,
            "title": title,
            "company": company,
            "company_linkedin_url": raw_job.get("company_linkedin_url") or None,

            "location": location,
            "remote": "remote" in location.lower(),

            "job_type": None,
            "seniority": None,

            "salary_min": None,
            "salary_max": None,
            "currency": None,

            "source": "LinkedIn",
            "job_url": job_url,

            "apply_type": apply_type,
            "apply_url": apply_url,
            "apply_label": apply_label,

            "poster_name": None,
            "poster_title": None,
            "poster_profile_url": None,

            "date_posted": str(date.today()),
        }

    def canonicalize_job_url(self, job_url: str, linkedin_job_id: str) -> str:
        if linkedin_job_id:
            return f"https://www.linkedin.com/jobs/view/{linkedin_job_id}/"

        extracted_job_id = self.extract_job_id(job_url)

        if extracted_job_id:
            return f"https://www.linkedin.com/jobs/view/{extracted_job_id}/"

        return job_url

    def extract_job_id(self, job_url: str, fallback: str = "") -> str:
        if fallback:
            return str(fallback).strip()

        if not job_url:
            return ""

        patterns = [
            r"/jobs/view/(\d+)",
            r"currentJobId=(\d+)",
            r"jobId=(\d+)",
        ]

        for pattern in patterns:
            match = re.search(pattern, job_url)

            if match:
                return match.group(1)

        return ""

    def absolute_url(self, url: str | None) -> str | None:
        if not url:
            return None

        if url.startswith("http://") or url.startswith("https://"):
            return url

        if url.startswith("/"):
            return f"https://www.linkedin.com{url}"

        return url

    def clean_text(self, value: str | None) -> str:
        if not value:
            return ""

        return re.sub(r"\s+", " ", value).strip()