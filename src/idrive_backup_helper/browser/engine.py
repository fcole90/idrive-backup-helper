from dataclasses import dataclass
from pathlib import Path
import subprocess
import time
from types import TracebackType
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import urlopen

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Error,
    Page,
    Playwright,
    sync_playwright,
)

DEFAULT_BROWSER_DEBUG_URL = "http://127.0.0.1:9222"
CDP_LAUNCH_TIMEOUT_SECONDS = 15.0
CDP_CONNECT_TIMEOUT_MS = 5_000


def _log(message: str) -> None:
    print(f"[browser-session] {message}", flush=True)


def _first_error_line(error: Error) -> str:
    return str(error).splitlines()[0]


@dataclass(frozen=True)
class BrowserConfig:
    profile_dir: Path
    downloads_dir: Path
    headless: bool
    timeout_ms: int
    browser_debug_url: str | None = None


class BrowserEngine:
    def __init__(self, config: BrowserConfig) -> None:
        self._config = config
        self._playwright_context = sync_playwright()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._browser_context: BrowserContext | None = None

    def __enter__(self) -> "BrowserEngine":
        self._config.profile_dir.mkdir(parents=True, exist_ok=True)
        self._config.downloads_dir.mkdir(parents=True, exist_ok=True)

        self._playwright = self._playwright_context.__enter__()
        if self._config.browser_debug_url is None:
            _log(
                "Launching owned persistent browser context "
                f"(headless={self._config.headless}, profile={self._config.profile_dir}, "
                f"downloads={self._config.downloads_dir})"
            )
            self._browser_context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self._config.profile_dir),
                headless=self._config.headless,
                accept_downloads=True,
                downloads_path=str(self._config.downloads_dir),
            )
        else:
            self._browser = self._connect_or_launch_browser(self._playwright)
            self._browser_context = self._default_browser_context(self._browser)
            _log(
                "Using detached browser context "
                f"with {len(self._browser_context.pages)} existing page(s)"
            )
        self._browser_context.set_default_timeout(self._config.timeout_ms)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._browser_context is not None and self._browser is None:
            _log("Closing owned browser context")
            self._browser_context.close()
        elif self._browser is not None:
            _log("Command finished; leaving detached browser running")

        self._playwright_context.__exit__(exc_type, exc, traceback)

    def new_page(self) -> Page:
        if self._browser_context is None:
            raise RuntimeError("BrowserEngine must be entered before creating a page.")

        _log("Opening new page in browser context")
        return self._browser_context.new_page()

    def current_page_or_new_page(self) -> Page:
        if self._browser_context is None:
            raise RuntimeError("BrowserEngine must be entered before selecting a page.")

        pages = self._browser_context.pages
        if pages:
            page = pages[-1]
            _log(f"Reusing existing browser page: {page.url}")
            return page

        _log("No existing browser page found; opening new page")
        return self._browser_context.new_page()

    def _connect_or_launch_browser(self, playwright: Playwright) -> Browser:
        if self._config.browser_debug_url is None:
            raise RuntimeError("Cannot attach without a browser debug endpoint.")

        _log(f"Connecting to Chromium CDP endpoint: {self._config.browser_debug_url}")
        try:
            browser = playwright.chromium.connect_over_cdp(
                self._config.browser_debug_url,
                timeout=CDP_CONNECT_TIMEOUT_MS,
            )
            _log(
                f"Attached to existing Chromium CDP endpoint: {self._config.browser_debug_url}"
            )
            return browser
        except Error as error:
            _log(
                "CDP connection failed; launching detached Chromium "
                f"({self._config.browser_debug_url}): {_first_error_line(error)}"
            )
            self._launch_detached_browser(playwright)
            _wait_for_cdp_endpoint(
                self._config.browser_debug_url,
                timeout_seconds=CDP_LAUNCH_TIMEOUT_SECONDS,
            )
            browser = playwright.chromium.connect_over_cdp(
                self._config.browser_debug_url,
                timeout=CDP_CONNECT_TIMEOUT_MS,
            )
            _log(
                f"Attached to launched Chromium CDP endpoint: {self._config.browser_debug_url}"
            )
            return browser

    def _launch_detached_browser(self, playwright: Playwright) -> None:
        if self._config.browser_debug_url is None:
            raise RuntimeError("Cannot launch detached browser without debug endpoint.")

        endpoint = _parse_local_debug_endpoint(self._config.browser_debug_url)
        args = [
            playwright.chromium.executable_path,
            f"--remote-debugging-address={endpoint.host}",
            f"--remote-debugging-port={endpoint.port}",
            f"--user-data-dir={self._config.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ]
        if self._config.headless:
            args.insert(1, "--headless=new")

        browser_process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        _log(
            "Detached Chromium launched "
            f"(pid={browser_process.pid}, profile={self._config.profile_dir})"
        )

    @staticmethod
    def _default_browser_context(browser: Browser) -> BrowserContext:
        contexts = browser.contexts
        if not contexts:
            raise RuntimeError("Connected browser did not expose a default context.")

        return contexts[0]


@dataclass(frozen=True)
class BrowserDebugEndpoint:
    host: str
    port: int


def _parse_local_debug_endpoint(endpoint_url: str) -> BrowserDebugEndpoint:
    parsed_url = urlparse(endpoint_url)
    if parsed_url.scheme not in {"http", "https"}:
        raise RuntimeError(
            "Browser debug endpoint must be an http(s) URL, " f"got: {endpoint_url}"
        )

    if parsed_url.hostname is None or parsed_url.port is None:
        raise RuntimeError(
            "Browser debug endpoint must include a host and port, "
            f"got: {endpoint_url}"
        )

    if parsed_url.hostname not in {"127.0.0.1", "localhost"}:
        raise RuntimeError(
            "Cannot launch a browser for a non-local debug endpoint: " f"{endpoint_url}"
        )

    return BrowserDebugEndpoint(host=parsed_url.hostname, port=parsed_url.port)


def _wait_for_cdp_endpoint(endpoint_url: str, *, timeout_seconds: float) -> None:
    version_url = endpoint_url.rstrip("/") + "/json/version"
    deadline = time.monotonic() + timeout_seconds
    last_error: OSError | URLError | TimeoutError | None = None

    _log(f"Waiting for CDP endpoint readiness: {version_url}")
    while time.monotonic() < deadline:
        try:
            with urlopen(version_url, timeout=1):
                _log(f"CDP endpoint is ready: {endpoint_url}")
                return
        except (OSError, URLError, TimeoutError) as error:
            last_error = error
            time.sleep(0.2)

    raise RuntimeError(
        "Timed out waiting for detached browser debug endpoint: "
        f"{endpoint_url} ({last_error})"
    )
