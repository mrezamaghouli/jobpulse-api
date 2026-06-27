from pathlib import Path
from playwright.sync_api import sync_playwright, Error as PlaywrightError

auth_dir = Path(".auth")
auth_dir.mkdir(exist_ok=True)

state_path = auth_dir / "linkedin_storage_state.json"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False, channel="chrome")
    context = browser.new_context()
    page = context.new_page()

    page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=60000)

    print("")
    print("Log in to LinkedIn manually in the opened browser.")
    print("After login, make sure you can see the LinkedIn feed page.")
    print("Then press ENTER here.")
    input()

    page.wait_for_timeout(5000)

    print("Current URL:", page.url)
    print("Title:", page.title())

    url = page.url.lower()
    html = page.content().lower()

    if "login" in url or "checkpoint" in url or "challenge" in url or "sign in" in html or "join now" in html:
        print("")
        print("WARNING: It still looks logged out or checkpointed.")
        print("Do not upload this file yet. Log in completely and run again.")
    else:
        context.storage_state(path=str(state_path))
        print("")
        print("Saved LinkedIn auth state to:", state_path)
        print("File size:", state_path.stat().st_size)

    browser.close()
