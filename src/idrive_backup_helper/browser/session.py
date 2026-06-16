from pathlib import Path

from playwright.sync_api import Page

from idrive_backup_helper.browser.engine import (
    BrowserConfig,
    BrowserEngine,
    DEFAULT_BROWSER_DEBUG_URL,
)

DEFAULT_AUTH_URL = "https://www.idrive.com/idrive/login/loginForm"
DEFAULT_BROWSE_URL = "https://www.idrive.com/idrive/home"


def _log(message: str) -> None:
    print(f"[browser-session] {message}", flush=True)


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

    _log(f"Interactive login required; current page is: {page.url}")
    input(
        "IDrive needs login/2FA in this browser window. Complete it there, "
        "then press Enter here to continue: "
    )

    if requires_login(page.url):
        _log(
            f"Still on login page after prompt; navigating back to target: {target_url}"
        )
        page.goto(target_url, wait_until="domcontentloaded")

    if requires_login(page.url):
        raise RuntimeError(
            "IDrive session is still not authenticated after interactive login."
        )


def login_and_save_state(
    profile_dir: Path,
    downloads_dir: Path,
    start_url: str = DEFAULT_AUTH_URL,
    browser_debug_url: str = DEFAULT_BROWSER_DEBUG_URL,
) -> None:
    open_authenticated_browser(
        profile_dir=profile_dir,
        downloads_dir=downloads_dir,
        start_url=start_url,
        browser_debug_url=browser_debug_url,
    )


def open_authenticated_browser(
    profile_dir: Path,
    downloads_dir: Path,
    start_url: str = DEFAULT_BROWSE_URL,
    browser_debug_url: str = DEFAULT_BROWSER_DEBUG_URL,
) -> None:
    config = BrowserConfig(
        profile_dir=profile_dir,
        downloads_dir=downloads_dir,
        headless=False,
        timeout_ms=120_000,
        browser_debug_url=browser_debug_url,
    )

    with BrowserEngine(config) as engine:
        page = engine.current_page_or_new_page()
        _log(f"Navigating browser to: {start_url}")
        page.goto(start_url, wait_until="domcontentloaded")
