import os
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg2
from psycopg2.extras import RealDictCursor
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from app.config import get_postgres_config, get_linkedin_browser


BASE_DIR = Path(__file__).resolve().parent.parent
AUTH_FILE = BASE_DIR / ".auth" / "linkedin_storage_state.json"


def get_company_enrich_limit() -> int:
    try:
        return int(os.getenv("COMPANY_ENRICH_LIMIT", "20"))
    except ValueError:
        return 20


def get_company_enrich_stale_days() -> int:
    try:
        return int(os.getenv("COMPANY_ENRICH_STALE_DAYS", "30"))
    except ValueError:
        return 30


def clean_text(value):
    if value is None:
        return None

    value = re.sub(r"\s+", " ", str(value)).strip()

    return value or None


def canonicalize_linkedin_company_url(url):
    if not url:
        return None

    url = str(url).strip()

    if not url:
        return None

    parsed = urlsplit(url)

    path = parsed.path.rstrip("/")

    match = re.search(r"/company/([^/]+)", path)

    if match:
        slug = match.group(1)
        return f"https://www.linkedin.com/company/{slug}/"

    return urlunsplit((parsed.scheme, parsed.netloc, path + "/", "", ""))


def build_company_about_url(company_url):
    canonical_url = canonicalize_linkedin_company_url(company_url)

    if not canonical_url:
        return None

    return canonical_url.rstrip("/") + "/about/"


def ensure_company_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS companies (
            id SERIAL PRIMARY KEY,
            linkedin_company_url TEXT UNIQUE,
            name TEXT,
            logo_url TEXT,
            website_url TEXT,
            industry TEXT,
            company_size TEXT,
            headquarters TEXT,
            about TEXT,
            last_enriched_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        ALTER TABLE jobs
        ADD COLUMN IF NOT EXISTS company_id INTEGER REFERENCES companies(id);

        CREATE INDEX IF NOT EXISTS idx_companies_linkedin_company_url
        ON companies(linkedin_company_url);

        CREATE INDEX IF NOT EXISTS idx_companies_last_enriched_at
        ON companies(last_enriched_at);
        """
    )


def fetch_companies_to_enrich(cursor, limit, stale_days):
    cursor.execute(
        """
        SELECT
            id,
            name,
            linkedin_company_url,
            logo_url,
            about,
            last_enriched_at
        FROM companies
        WHERE linkedin_company_url IS NOT NULL
          AND linkedin_company_url != ''
          AND (
                last_enriched_at IS NULL
                OR last_enriched_at < CURRENT_TIMESTAMP - (%s * INTERVAL '1 day')
                OR about IS NULL
                OR logo_url IS NULL
          )
        ORDER BY
            last_enriched_at ASC NULLS FIRST,
            updated_at DESC NULLS LAST
        LIMIT %s;
        """,
        (
            stale_days,
            limit,
        ),
    )

    return cursor.fetchall()


def launch_browser(playwright):
    browser_channel = get_linkedin_browser()

    supported_channels = {
        "chrome": "chrome",
        "edge": "msedge",
        "msedge": "msedge",
    }

    channel = supported_channels.get(browser_channel, "chrome")

    print(f"Launching browser channel: {channel}")

    return playwright.chromium.launch(
        channel=channel,
        headless=False,
        slow_mo=150,
    )


def open_company_page(page, url):
    page.set_default_timeout(30000)
    page.set_default_navigation_timeout(90000)

    try:
        page.goto(
            url,
            wait_until="commit",
            timeout=90000,
        )
        page.wait_for_timeout(5000)

    except PlaywrightTimeoutError as error:
        print(f"Company page navigation timeout: {error}")
        print("Trying to continue with current page state...")

        if "linkedin.com" in (page.url or ""):
            page.wait_for_timeout(5000)
            return

        raise


def extract_company_info_from_page(page):
    return page.evaluate(
        """
        () => {
            const clean = (value) => {
                if (!value) {
                    return null;
                }

                const cleaned = String(value).replace(/\\s+/g, ' ').trim();

                return cleaned || null;
            };

            const getText = (selectors) => {
                for (const selector of selectors) {
                    const el = document.querySelector(selector);

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

                return null;
            };

            const getLogoUrl = () => {
                const selectors = [
                    '.org-top-card-primary-content__logo-container img',
                    '.org-top-card-primary-content__logo img',
                    '.org-top-card-summary__logo img',
                    '.org-top-card__logo img',
                    'img[alt*="logo"]',
                    'img[alt*="Logo"]',
                    'img[src*="media.licdn.com"]'
                ];

                for (const selector of selectors) {
                    const el = document.querySelector(selector);

                    if (!el) {
                        continue;
                    }

                    const src =
                        el.src ||
                        el.getAttribute('src') ||
                        el.getAttribute('data-delayed-url') ||
                        el.getAttribute('data-ghost-url') ||
                        null;

                    if (src) {
                        return src;
                    }
                }

                return null;
            };

            const getDetailByLabel = (labels) => {
                const normalizedLabels = labels.map(label => label.toLowerCase());

                const dts = Array.from(document.querySelectorAll('dt'));

                for (const dt of dts) {
                    const labelText = clean(dt.innerText);

                    if (!labelText) {
                        continue;
                    }

                    const lowerLabel = labelText.toLowerCase();

                    const matched = normalizedLabels.some(label => lowerLabel.includes(label));

                    if (!matched) {
                        continue;
                    }

                    let dd = dt.nextElementSibling;

                    while (dd && dd.tagName && dd.tagName.toLowerCase() !== 'dd') {
                        dd = dd.nextElementSibling;
                    }

                    if (dd && clean(dd.innerText)) {
                        return clean(dd.innerText);
                    }
                }

                const allText = document.body ? document.body.innerText : '';
                const lines = allText
                    .split('\\n')
                    .map(line => clean(line))
                    .filter(Boolean);

                for (let index = 0; index < lines.length; index += 1) {
                    const lowerLine = lines[index].toLowerCase();

                    const matched = normalizedLabels.some(label => lowerLine === label || lowerLine.includes(label));

                    if (matched && lines[index + 1]) {
                        return lines[index + 1];
                    }
                }

                return null;
            };

            const getWebsiteUrl = () => {
                const selectors = [
                    'a[data-control-name*="website"]',
                    'a[href^="http"]:not([href*="linkedin.com"])'
                ];

                for (const selector of selectors) {
                    const el = document.querySelector(selector);

                    if (el && el.href && !el.href.includes('linkedin.com')) {
                        return el.href;
                    }
                }

                return null;
            };

            const name = getText([
                'h1.org-top-card-summary__title',
                '.org-top-card-summary__title',
                'h1'
            ]);

            const about = getText([
                '.org-about-us-organization-description__text',
                '.org-about-module__description',
                'section.org-about-module p',
                '.break-words.white-space-pre-wrap',
                '.org-page-details__definition-text'
            ]);

            const logoUrl = getLogoUrl();

            const websiteUrl =
                getWebsiteUrl() ||
                getDetailByLabel(['website']);

            const industry = getDetailByLabel([
                'industry',
                'branche'
            ]);

            const companySize = getDetailByLabel([
                'company size',
                'größe',
                'employees',
                'beschäftigte'
            ]);

            const headquarters = getDetailByLabel([
                'headquarters',
                'hauptsitz',
                'location'
            ]);

            return {
                name,
                logo_url: logoUrl,
                website_url: websiteUrl,
                industry,
                company_size: companySize,
                headquarters,
                about
            };
        }
        """
    )


def update_company(cursor, company_id, company_info):
    cursor.execute(
        """
        UPDATE companies
        SET
            name = COALESCE(%s, name),
            logo_url = COALESCE(%s, logo_url),
            website_url = COALESCE(%s, website_url),
            industry = COALESCE(%s, industry),
            company_size = COALESCE(%s, company_size),
            headquarters = COALESCE(%s, headquarters),
            about = COALESCE(%s, about),
            last_enriched_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s;
        """,
        (
            clean_text(company_info.get("name")),
            clean_text(company_info.get("logo_url")),
            clean_text(company_info.get("website_url")),
            clean_text(company_info.get("industry")),
            clean_text(company_info.get("company_size")),
            clean_text(company_info.get("headquarters")),
            clean_text(company_info.get("about")),
            company_id,
        ),
    )


def enrich_companies_from_linkedin():
    if not AUTH_FILE.exists():
        raise FileNotFoundError(
            f"LinkedIn auth session not found: {AUTH_FILE}. "
            "Run scripts/linkedin_save_session_from_browser.py first."
        )

    limit = get_company_enrich_limit()
    stale_days = get_company_enrich_stale_days()

    connection = psycopg2.connect(**get_postgres_config())

    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            ensure_company_table(cursor)

            companies = fetch_companies_to_enrich(
                cursor=cursor,
                limit=limit,
                stale_days=stale_days,
            )

            connection.commit()

        if not companies:
            print("No companies need enrichment.")
            return

        print(f"Companies to enrich: {len(companies)}")
        print(f"Stale days: {stale_days}")
        print(f"Limit: {limit}")

        enriched_count = 0
        failed_count = 0

        with sync_playwright() as playwright:
            browser = launch_browser(playwright)

            context = browser.new_context(
                storage_state=str(AUTH_FILE),
                viewport={
                    "width": 1366,
                    "height": 900,
                },
            )

            page = context.new_page()

            for index, company in enumerate(companies, start=1):
                company_id = company["id"]
                company_name = company.get("name") or "Unknown company"
                company_url = company.get("linkedin_company_url")
                about_url = build_company_about_url(company_url)

                print("\n" + "=" * 70)
                print(f"Company {index}/{len(companies)}")
                print(f"Name: {company_name}")
                print(f"URL: {company_url}")
                print(f"About URL: {about_url}")

                if not about_url:
                    failed_count += 1
                    continue

                try:
                    open_company_page(page, about_url)

                    current_url = page.url or ""

                    if "/login" in current_url or "checkpoint" in current_url:
                        print("LinkedIn asked for login/checkpoint.")
                        print("Complete it manually in the opened browser window.")
                        input("Press ENTER after the company page is visible... ")

                    company_info = extract_company_info_from_page(page)

                    print(f"Extracted name: {company_info.get('name')}")
                    print(f"Logo: {company_info.get('logo_url')}")
                    print(f"Website: {company_info.get('website_url')}")
                    print(f"Industry: {company_info.get('industry')}")
                    print(f"Size: {company_info.get('company_size')}")
                    print(f"Headquarters: {company_info.get('headquarters')}")
                    print(
                        "About preview: "
                        f"{(company_info.get('about') or '')[:160]}"
                    )

                    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                        update_company(
                            cursor=cursor,
                            company_id=company_id,
                            company_info=company_info,
                        )
                        connection.commit()

                    enriched_count += 1

                    page.wait_for_timeout(2500)

                except Exception as error:
                    print(f"Failed to enrich company {company_id}: {error}")
                    failed_count += 1

            browser.close()

        print("\n" + "=" * 70)
        print("Company enrichment finished.")
        print(f"Enriched companies: {enriched_count}")
        print(f"Failed companies: {failed_count}")

    finally:
        connection.close()


if __name__ == "__main__":
    enrich_companies_from_linkedin()