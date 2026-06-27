import argparse
import csv
import json
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

from playwright.sync_api import sync_playwright


def extract_job_id(url: str) -> str:
    if not url:
        return ""

    patterns = [
        r"/jobs/view/(?:[^/?#]+-)?(\d+)(?:[/?#]|$)",
        r"/jobs/view/(\d+)(?:[/?#]|$)",
        r"currentJobId=(\d+)",
        r"jobId=(\d+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)

    return ""


def canonical_job_url(job_url: str, job_id: str) -> str:
    if job_id:
        return f"https://www.linkedin.com/jobs/view/{job_id}/"
    return job_url or ""


def is_bad_title(title: str) -> bool:
    title = (title or "").strip()
    if not title:
        return True

    if re.match(r"^\d+\s+.+\s+jobs\s+in\s+.+$", title, re.IGNORECASE):
        return True

    if title.lower().endswith(" jobs in australia"):
        return True

    if title.lower().endswith(" jobs in united states"):
        return True

    if title.lower().endswith(" jobs in canada"):
        return True

    return False


def detect_login_state(page) -> str:
    url = (page.url or "").lower()

    try:
        title = (page.title() or "").lower()
    except Exception:
        title = ""

    try:
        body = (page.locator("body").inner_text(timeout=5000) or "").lower()
    except Exception:
        body = ""

    if "checkpoint" in url or "challenge" in url or "security verification" in title:
        return "checkpoint"

    if "linkedin.com/login" in url or "sign in" in title:
        return "logged_out"

    if "sign in" in body and "join now" in body:
        return "logged_out"

    return "logged_in"


def clean_text(value) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def collect_from_search_page(page, limit: int) -> list[dict]:
    rows = page.evaluate(
        """
        (limit) => {
            const textOf = (el) => el ? (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim() : '';

            const cardSelectors = [
                'li[data-occludable-job-id]',
                'div.job-card-container',
                'li.jobs-search-results__list-item',
                'div[data-job-id]',
                '.job-card-list__entity-lockup',
                '.base-card'
            ];

            let cards = [];
            for (const selector of cardSelectors) {
                const found = Array.from(document.querySelectorAll(selector));
                if (found.length > cards.length) cards = found;
            }

            if (!cards.length) {
                cards = Array.from(document.querySelectorAll('a[href*="/jobs/view/"]')).map(a => a.closest('li, div') || a);
            }

            const results = [];

            for (const card of cards) {
                const link = card.querySelector('a[href*="/jobs/view/"]') || (card.matches && card.matches('a[href*="/jobs/view/"]') ? card : null);
                const href = link ? link.href : '';

                const titleEl =
                    card.querySelector('.job-card-list__title') ||
                    card.querySelector('.job-card-list__title--link') ||
                    card.querySelector('.base-search-card__title') ||
                    card.querySelector('[data-test-job-title]') ||
                    card.querySelector('a[href*="/jobs/view/"] strong') ||
                    card.querySelector('a[href*="/jobs/view/"] span[aria-hidden="true"]') ||
                    card.querySelector('a[href*="/jobs/view/"]');

                const companyEl =
                    card.querySelector('.job-card-container__primary-description') ||
                    card.querySelector('.job-card-container__company-name') ||
                    card.querySelector('.base-search-card__subtitle') ||
                    card.querySelector('.artdeco-entity-lockup__subtitle');

                const locationEl =
                    card.querySelector('.job-card-container__metadata-item') ||
                    card.querySelector('.job-search-card__location') ||
                    card.querySelector('.base-search-card__metadata') ||
                    card.querySelector('.artdeco-entity-lockup__caption');

                results.push({
                    title: textOf(titleEl),
                    company: textOf(companyEl),
                    location: textOf(locationEl),
                    job_url: href
                });

                if (results.length >= limit) break;
            }

            return results;
        }
        """,
        limit,
    )

    seen = set()
    clean_rows = []

    for row in rows:
        job_url = row.get("job_url") or ""
        job_id = extract_job_id(job_url)
        title = clean_text(row.get("title"))
        company = clean_text(row.get("company"))
        location = clean_text(row.get("location"))

        if is_bad_title(title):
            continue

        key = job_id or job_url
        if not key or key in seen:
            continue

        seen.add(key)

        clean_rows.append(
            {
                "linkedin_job_id": job_id,
                "title": title,
                "company": company,
                "location": location,
                "job_url": canonical_job_url(job_url, job_id),
                "raw_job_url": job_url,
                "source": "linkedin",
            }
        )

    return clean_rows


def enrich_detail(context, job: dict, timeout_ms: int = 30000) -> dict:
    url = job.get("job_url") or job.get("raw_job_url")
    if not url:
        return job

    page = context.new_page()

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(2500)

        state = detect_login_state(page)
        if state != "logged_in":
            job["detail_error"] = f"detail_page_{state}"
            page.close()
            return job

        data = page.evaluate(
            """
            () => {
                const textOf = (el) => el ? (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim() : '';

                const titleEl =
                    document.querySelector('.jobs-unified-top-card__job-title') ||
                    document.querySelector('.job-details-jobs-unified-top-card__job-title') ||
                    document.querySelector('h1');

                const companyEl =
                    document.querySelector('.jobs-unified-top-card__company-name a') ||
                    document.querySelector('.jobs-unified-top-card__company-name') ||
                    document.querySelector('.job-details-jobs-unified-top-card__company-name a') ||
                    document.querySelector('.topcard__org-name-link');

                const descriptionEl =
                    document.querySelector('#job-details') ||
                    document.querySelector('.jobs-description__content') ||
                    document.querySelector('.show-more-less-html__markup');

                const locationEl =
                    document.querySelector('.jobs-unified-top-card__bullet') ||
                    document.querySelector('.job-details-jobs-unified-top-card__primary-description-container') ||
                    document.querySelector('.topcard__flavor--bullet');

                const applyEl =
                    document.querySelector('a[href*="companyApplyUrl"]') ||
                    document.querySelector('a[href*="/jobs/view/externalApply"]') ||
                    document.querySelector('a[data-control-name*="jobdetails_topcard"]');

                return {
                    detail_title: textOf(titleEl),
                    detail_company: textOf(companyEl),
                    detail_location: textOf(locationEl),
                    description: textOf(descriptionEl),
                    apply_url: applyEl ? applyEl.href : ''
                };
            }
            """
        )

        if data.get("detail_title") and not is_bad_title(data["detail_title"]):
            job["title"] = data["detail_title"]

        if data.get("detail_company"):
            job["company"] = data["detail_company"]

        if data.get("detail_location"):
            job["location"] = data["detail_location"]

        job["job_description"] = data.get("description", "")
        job["apply_url"] = data.get("apply_url", "")
        job["detail_error"] = ""

    except Exception as exc:
        job["detail_error"] = str(exc)[:300]

    finally:
        page.close()

    return job


def save_outputs(rows: list[dict], out_dir: Path, keywords: str, location: str) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_keywords = re.sub(r"[^a-zA-Z0-9]+", "_", keywords).strip("_").lower()
    safe_location = re.sub(r"[^a-zA-Z0-9]+", "_", location).strip("_").lower()

    json_path = out_dir / f"linkedin_export_{safe_keywords}_{safe_location}_{stamp}.json"
    csv_path = out_dir / f"linkedin_export_{safe_keywords}_{safe_location}_{stamp}.csv"

    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    fieldnames = [
        "linkedin_job_id",
        "title",
        "company",
        "location",
        "job_url",
        "raw_job_url",
        "apply_url",
        "job_description",
        "source",
        "detail_error",
    ]

    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    return json_path, csv_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keywords", required=True)
    parser.add_argument("--location", required=True)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--state", default=".auth/linkedin_storage_state.json")
    parser.add_argument("--out-dir", default="exports")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--skip-detail", action="store_true")
    args = parser.parse_args()

    state_path = Path(args.state)
    if not state_path.exists():
        raise SystemExit(f"Storage state not found: {state_path}")

    f_tpr = f"r{args.lookback_days * 86400}"
    search_url = (
        "https://www.linkedin.com/jobs/search/"
        f"?keywords={quote_plus(args.keywords)}"
        f"&location={quote_plus(args.location)}"
        f"&f_TPR={f_tpr}"
        "&sortBy=DD"
    )

    print("LinkedIn local export started")
    print("keywords:", args.keywords)
    print("location:", args.location)
    print("limit:", args.limit)
    print("search_url:", search_url)
    print("state:", state_path, "size:", state_path.stat().st_size)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=args.headless,
            channel="chrome",
            args=["--disable-dev-shm-usage"],
        )

        context = browser.new_context(storage_state=str(state_path))
        page = context.new_page()

        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        login_state = detect_login_state(page)
        print("login_state:", login_state)
        print("feed_url:", page.url)
        print("feed_title:", page.title())

        if login_state != "logged_in":
            raise SystemExit("LinkedIn local auth is not logged in. Run scripts\\refresh_linkedin_auth.py first.")

        page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)

        all_rows = []
        seen = set()

        for scroll_idx in range(1, 8):
            rows = collect_from_search_page(page, args.limit)

            for row in rows:
                key = row.get("linkedin_job_id") or row.get("job_url")
                if key and key not in seen:
                    seen.add(key)
                    all_rows.append(row)

            print(f"scroll {scroll_idx}: collected={len(all_rows)}")

            if len(all_rows) >= args.limit:
                break

            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(2500)

        all_rows = all_rows[: args.limit]

        if not args.skip_detail:
            enriched = []
            for i, job in enumerate(all_rows, start=1):
                print(f"detail {i}/{len(all_rows)}: {job.get('title')} | {job.get('company')}")
                enriched.append(enrich_detail(context, job))
                time.sleep(0.5)
            all_rows = enriched

        context.close()
        browser.close()

    json_path, csv_path = save_outputs(all_rows, Path(args.out_dir), args.keywords, args.location)

    print("")
    print("Export finished")
    print("jobs:", len(all_rows))
    print("json:", json_path)
    print("csv:", csv_path)

    print("")
    print("First rows:")
    for row in all_rows[:10]:
        print("-", row.get("title"), "|", row.get("company"), "|", row.get("location"), "|", row.get("job_url"))


if __name__ == "__main__":
    main()
