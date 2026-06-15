import os
import re
import traceback
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
    return page.evaluate(r"""
        () => {
            const clean = (value) => {
                if (!value || typeof value !== 'string') {
                    return null;
                }

                const cleaned = value
                    .replace(/\s+/g, ' ')
                    .replace(/^\s+|\s+$/g, '');

                return cleaned || null;
            };

            const getTextBySelectors = (selectors) => {
                for (const selector of selectors) {
                    const el = document.querySelector(selector);

                    if (!el) {
                        continue;
                    }

                    const text = clean(el.innerText || el.textContent);

                    if (text) {
                        return text;
                    }
                }

                return null;
            };

            const normalizeUrl = (url) => {
                if (!url || typeof url !== 'string') {
                    return null;
                }

                const cleaned = url.trim();

                if (
                    !cleaned ||
                    cleaned.startsWith('data:') ||
                    cleaned.startsWith('blob:') ||
                    cleaned.includes('ghost') ||
                    cleaned.includes('transparent') ||
                    cleaned.includes('spacer') ||
                    cleaned.includes('static.licdn.com/scds/common/u/images')
                ) {
                    return null;
                }

                try {
                    return new URL(cleaned, window.location.origin).href;
                } catch (error) {
                    return cleaned;
                }
            };

            const getImageUrl = (img) => {
                if (!img) {
                    return null;
                }

                const directValues = [
                    img.currentSrc,
                    img.src,
                    img.getAttribute('src'),
                    img.getAttribute('data-delayed-url'),
                    img.getAttribute('data-src'),
                    img.getAttribute('data-lazy-src')
                ];

                for (const value of directValues) {
                    const url = normalizeUrl(value);

                    if (url) {
                        return url;
                    }
                }

                const srcset = img.getAttribute('srcset') || img.getAttribute('data-srcset') || '';

                if (srcset) {
                    const srcsetCandidates = srcset
                        .split(',')
                        .map((item) => item.trim().split(/\s+/)[0])
                        .filter(Boolean);

                    for (const candidate of srcsetCandidates.reverse()) {
                        const url = normalizeUrl(candidate);

                        if (url) {
                            return url;
                        }
                    }
                }

                return null;
            };

            const scoreImage = (img, url) => {
                const alt = (img.getAttribute('alt') || '').toLowerCase();
                const className = (img.getAttribute('class') || '').toLowerCase();
                const parentClassName = img.parentElement
                    ? (img.parentElement.getAttribute('class') || '').toLowerCase()
                    : '';

                const text = [
                    url,
                    alt,
                    className,
                    parentClassName
                ].join(' ').toLowerCase();

                let score = 0;

                if (url.includes('media.licdn.com')) score += 20;
                if (url.includes('/dms/image/')) score += 20;
                if (url.includes('company-logo')) score += 70;

                if (text.includes('logo')) score += 55;
                if (text.includes('company')) score += 30;
                if (text.includes('org-top-card')) score += 40;
                if (text.includes('entityphoto-square')) score += 35;
                if (text.includes('profile-displayphoto')) score += 15;

                const width = img.naturalWidth || img.width || 0;
                const height = img.naturalHeight || img.height || 0;

                if (width >= 40 && height >= 40) score += 10;
                if (width > 600 || height > 600) score -= 35;

                if (
                    text.includes('banner') ||
                    text.includes('background') ||
                    text.includes('cover')
                ) {
                    score -= 45;
                }

                return score;
            };

            const getLogoUrl = () => {
                const selectors = [
                    '.org-top-card-primary-content__logo-container img',
                    '.org-top-card-primary-content__logo img',
                    '.org-top-card-summary__logo img',
                    '.org-top-card__logo img',
                    '.org-company-logo img',
                    '.org-page-navigation__logo img',
                    'img.EntityPhoto-square-3',
                    'img.EntityPhoto-square-4',
                    '.EntityPhoto-square-3 img',
                    '.EntityPhoto-square-4 img',
                    'img[alt*="logo"]',
                    'img[alt*="Logo"]',
                    'img[alt*="company"]',
                    'img[alt*="Company"]',
                    'img[src*="company-logo"]',
                    'img[src*="media.licdn.com/dms/image"]',
                    'img[data-delayed-url*="media.licdn.com"]'
                ];

                const candidates = [];

                for (const selector of selectors) {
                    const images = Array.from(document.querySelectorAll(selector));

                    for (const img of images) {
                        const url = getImageUrl(img);

                        if (!url || !url.includes('media.licdn.com')) {
                            continue;
                        }

                        candidates.push({
                            url: url,
                            score: scoreImage(img, url) + 20
                        });
                    }
                }

                const allImages = Array.from(document.querySelectorAll('img'));

                for (const img of allImages) {
                    const url = getImageUrl(img);

                    if (!url || !url.includes('media.licdn.com')) {
                        continue;
                    }

                    const score = scoreImage(img, url);

                    if (score >= 35) {
                        candidates.push({
                            url: url,
                            score: score
                        });
                    }
                }

                const metaImage = normalizeUrl(
                    document.querySelector('meta[property="og:image"]')?.getAttribute('content') ||
                    document.querySelector('meta[name="twitter:image"]')?.getAttribute('content')
                );

                if (metaImage && metaImage.includes('media.licdn.com')) {
                    candidates.push({
                        url: metaImage,
                        score: 25
                    });
                }

                const unique = new Map();

                for (const item of candidates) {
                    const existing = unique.get(item.url);

                    if (!existing || item.score > existing.score) {
                        unique.set(item.url, item);
                    }
                }

                const ranked = Array.from(unique.values())
                    .sort((a, b) => b.score - a.score);

                return ranked.length ? ranked[0].url : null;
            };

            const getWebsiteUrl = () => {
                const links = Array.from(document.querySelectorAll('a[href]'));

                for (const link of links) {
                    const href = link.getAttribute('href');

                    if (!href) {
                        continue;
                    }

                    const text = (link.innerText || link.textContent || '').toLowerCase();
                    const aria = (link.getAttribute('aria-label') || '').toLowerCase();
                    const combined = `${href} ${text} ${aria}`.toLowerCase();

                    if (
                        combined.includes('website') ||
                        combined.includes('visit website') ||
                        combined.includes('company website')
                    ) {
                        return normalizeUrl(href);
                    }
                }

                return null;
            };

            const getDetailByLabel = (labels) => {
                const normalizedLabels = labels.map((label) => label.toLowerCase());

                const nodes = Array.from(document.querySelectorAll('dt, h3, div, span'));

                for (const node of nodes) {
                    const labelText = clean(node.innerText || node.textContent);

                    if (!labelText) {
                        continue;
                    }

                    const normalizedLabelText = labelText.toLowerCase();

                    const matched = normalizedLabels.some((label) => {
                        return normalizedLabelText === label ||
                            normalizedLabelText.includes(label);
                    });

                    if (!matched) {
                        continue;
                    }

                    const parent = node.parentElement;

                    if (!parent) {
                        continue;
                    }

                    const valueCandidates = Array.from(parent.querySelectorAll('dd, span, div, a'))
                        .map((el) => clean(el.innerText || el.textContent))
                        .filter(Boolean)
                        .filter((value) => value.toLowerCase() !== normalizedLabelText);

                    if (valueCandidates.length) {
                        return valueCandidates[valueCandidates.length - 1];
                    }
                }

                return null;
            };

            const name =
                getTextBySelectors([
                    'h1',
                    '.org-top-card-summary__title',
                    '.org-top-card-primary-content__title',
                    '.org-top-card__primary-content h1'
                ]);

            const about =
                getTextBySelectors([
                    '.org-about-us-organization-description__text',
                    '.break-words.white-space-pre-wrap',
                    '.org-page-details__definition-text',
                    'section p'
                ]);

            const logoUrl = getLogoUrl();

            const websiteUrl =
                getWebsiteUrl() ||
                getDetailByLabel(['website']);

            const industry = getDetailByLabel([
                'industry',
                'industries'
            ]);

            const companySize = getDetailByLabel([
                'company size',
                'size'
            ]);

            const headquarters = getDetailByLabel([
                'headquarters',
                'headquarter'
            ]);

            return {
                name: name,
                logo_url: logoUrl,
                website_url: websiteUrl,
                industry: industry,
                company_size: companySize,
                headquarters: headquarters,
                about: about
            };
        }
    """)


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