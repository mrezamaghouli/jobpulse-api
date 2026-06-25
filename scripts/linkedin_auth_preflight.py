from pathlib import Path
from playwright.sync_api import sync_playwright
from scripts.providers.linkedin_browser_provider import assert_linkedin_authenticated


def preflight_linkedin_auth():
    import os

    enabled = os.getenv("LINKEDIN_AUTH_PREFLIGHT", "true").lower() not in ("0", "false", "no")
    if not enabled:
        print("LinkedIn auth preflight skipped by LINKEDIN_AUTH_PREFLIGHT=false")
        return

    state_path = os.getenv("LINKEDIN_STORAGE_STATE", "/app/.auth/linkedin_storage_state.json")

    state_file = Path(state_path)
    if not state_file.exists() or state_file.stat().st_size <= 0:
        raise SystemExit(
            f"LINKEDIN_AUTH_PREFLIGHT_FAILED: storage state missing or empty: {state_path}"
        )

    print("Running LinkedIn auth preflight check...")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                channel=os.getenv("LINKEDIN_BROWSER_CHANNEL", "chrome"),
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )

            context = browser.new_context(storage_state=state_path)
            page = context.new_page()

            page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)

            assert_linkedin_authenticated(page, stage="process_search_demand_queue_preflight")

            context.close()
            browser.close()

        print("LinkedIn auth preflight passed.")

    except Exception as exc:
        raise SystemExit(f"LINKEDIN_AUTH_PREFLIGHT_FAILED: {exc}")
