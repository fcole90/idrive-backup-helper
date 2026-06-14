from pathlib import Path

from playwright.sync_api import sync_playwright

DEFAULT_AUTH_URL = "https://www.idrive.com/idrive/login/loginForm"
DEFAULT_BROWSE_URL = "https://www.idrive.com/idrive/home"


def login_and_save_state(profile_dir: Path, start_url: str = DEFAULT_AUTH_URL) -> None:
    profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            accept_downloads=True,
        )

        page = context.pages[0] if context.pages else context.new_page()
        page.goto(start_url, wait_until="domcontentloaded")

        input("Complete IDrive login in the browser, then press Enter here: ")
        context.close()


def open_authenticated_browser(
    profile_dir: Path,
    start_url: str = DEFAULT_BROWSE_URL,
) -> None:
    if not profile_dir.exists():
        raise RuntimeError("Missing browser auth state. Run: uv run main auth")

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            accept_downloads=True,
        )

        page = context.pages[0] if context.pages else context.new_page()
        page.goto(start_url, wait_until="domcontentloaded")

        input("Browse IDrive in the browser, then press Enter here to close it: ")
        context.close()
