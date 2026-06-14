from pathlib import Path

from playwright.sync_api import Page, sync_playwright

DEFAULT_AUTH_URL = "https://www.idrive.com/idrive/login/loginForm"
DEFAULT_BROWSE_URL = "https://www.idrive.com/idrive/home"


def requires_login(url: str) -> bool:
    lowered_url = url.lower()
    return "/login/" in lowered_url or "loginform" in lowered_url


def ensure_authenticated_page(
    page: Page,
    *,
    target_url: str,
    allow_interactive_login: bool,
) -> None:
    if not requires_login(page.url):
        return

    if not allow_interactive_login:
        raise RuntimeError(
            "IDrive session requires interactive login for this browser launch. "
            "Re-run with --headed and complete 2FA in the opened browser."
        )

    input(
        "IDrive needs login/2FA in this browser window. Complete it there, "
        "then press Enter here to continue: "
    )

    if requires_login(page.url):
        page.goto(target_url, wait_until="domcontentloaded")

    if requires_login(page.url):
        raise RuntimeError(
            "IDrive session is still not authenticated after interactive login."
        )


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
