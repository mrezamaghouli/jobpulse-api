from pathlib import Path

from playwright.sync_api import sync_playwright


BASE_DIR = Path(__file__).resolve().parent.parent
AUTH_DIR = BASE_DIR / ".auth"
AUTH_FILE = AUTH_DIR / "linkedin_storage_state.json"


def save_session_from_running_browser():
    AUTH_DIR.mkdir(exist_ok=True)

    with sync_playwright() as playwright:
        print("Connecting to running browser on http://127.0.0.1:9222 ...")

        browser = playwright.chromium.connect_over_cdp(
            "http://127.0.0.1:9222"
        )

        if not browser.contexts:
            raise RuntimeError(
                "No browser context found. Make sure Chrome or Edge is open with remote debugging."
            )

        context = browser.contexts[0]

        if context.pages:
            page = context.pages[0]
        else:
            page = context.new_page()

        print("Opening LinkedIn feed...")
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")

        print("\nMake sure you are logged in to LinkedIn in the opened browser window.")
        print("If LinkedIn asks for login, complete it manually.")
        input("Press ENTER here after LinkedIn is fully logged in... ")

        current_url = page.url
        print(f"Current URL: {current_url}")

        context.storage_state(path=str(AUTH_FILE))

        print("\nLinkedIn session saved successfully.")
        print(f"Session file: {AUTH_FILE}")
        print("\nDo NOT commit the .auth folder to GitHub.")

        browser.close()


if __name__ == "__main__":
    save_session_from_running_browser()