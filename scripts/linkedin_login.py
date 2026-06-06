import os
from pathlib import Path

from playwright.sync_api import sync_playwright


BASE_DIR = Path(__file__).resolve().parent.parent
AUTH_DIR = BASE_DIR / ".auth"
PROFILE_DIR = AUTH_DIR / "linkedin_browser_profile"
AUTH_FILE = AUTH_DIR / "linkedin_storage_state.json"


def get_browser_channel():
    browser_name = os.getenv("LINKEDIN_LOGIN_BROWSER", "msedge").strip().lower()

    supported_channels = {
        "edge": "msedge",
        "msedge": "msedge",
        "chrome": "chrome",
    }

    return supported_channels.get(browser_name, "msedge")


def save_linkedin_login_session():
    AUTH_DIR.mkdir(exist_ok=True)
    PROFILE_DIR.mkdir(exist_ok=True)

    browser_channel = get_browser_channel()

    print(f"Launching persistent browser profile with: {browser_channel}")
    print(f"Profile directory: {PROFILE_DIR}")

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel=browser_channel,
            headless=False,
            slow_mo=300,
            viewport={
                "width": 1366,
                "height": 900
            }
        )

        page = context.new_page()

        print("Opening LinkedIn login page...")
        page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")

        print("\nPlease log in manually in the opened browser window.")
        print("Complete CAPTCHA / 2FA manually if LinkedIn asks for it.")
        print("If the CAPTCHA/code area is blank, try refreshing the page once.")
        print("Do not close the browser window before pressing ENTER here.")
        print("After you successfully log in and see LinkedIn home/feed, come back here.")
        input("Press ENTER here after login is complete... ")

        current_url = page.url

        if "linkedin.com" not in current_url:
            print("Warning: current page does not look like LinkedIn.")
            print(f"Current URL: {current_url}")

        context.storage_state(path=str(AUTH_FILE))

        print("\nLinkedIn login session saved successfully.")
        print(f"Session file: {AUTH_FILE}")
        print(f"Persistent profile: {PROFILE_DIR}")
        print("\nDo NOT commit the .auth folder to GitHub.")

        context.close()


if __name__ == "__main__":
    save_linkedin_login_session()