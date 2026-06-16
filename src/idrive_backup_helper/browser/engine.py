import os
import socket
import subprocess
import time
from pathlib import Path
from dataclasses import dataclass
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
PROFILE_SINGLETON_FILE_NAMES = (
    "SingletonCookie",
    "SingletonLock",
    "SingletonSocket",
)
DETACHED_CHROMIUM_STARTUP_FLAGS = (
    "--no-sandbox",
    "--no-first-run",
    "--no-default-browser-check",
)


def _log(message: str) -> None:
    print(f"[browser-session] {message}", flush=True)


def _first_error_line(error: Error) -> str:
    return str(error).splitlines()[0]


def ensure_browser_executable(executable_path: Path) -> Path:
    if executable_path.exists():
        return executable_path

    raise RuntimeError(
        "Missing Playwright-managed Chromium executable: "
        f"{executable_path}. Run: uv run poe browser-setup"
    )


def ensure_playwright_chromium_executable(playwright: Playwright) -> Path:
    executable_path = ensure_browser_executable(
        Path(playwright.chromium.executable_path)
    )
    _log(
        "Using Playwright-managed Chromium package executable "
        f"(Linux binary name may be 'chrome'): {executable_path}"
    )
    return executable_path


def remove_stale_browser_profile_lock_files(profile_dir: Path) -> list[Path]:
    lock_pid = _browser_profile_lock_pid(profile_dir / "SingletonLock")
    if lock_pid is None or _process_is_running(lock_pid):
        return []

    removed_paths: list[Path] = []
    for file_name in PROFILE_SINGLETON_FILE_NAMES:
        lock_path = profile_dir / file_name
        try:
            lock_path.unlink()
        except FileNotFoundError:
            continue
        removed_paths.append(lock_path)

    return removed_paths


def _browser_profile_lock_pid(lock_path: Path) -> int | None:
    try:
        lock_target = (
            str(lock_path.readlink())
            if lock_path.is_symlink()
            else lock_path.read_text(encoding="utf-8").strip()
        )
    except OSError, UnicodeDecodeError:
        return None

    lock_host, separator, lock_pid = lock_target.rpartition("-")
    if separator == "" or lock_host != socket.gethostname() or not lock_pid.isdecimal():
        return None

    return int(lock_pid)


def _process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

    return True


@dataclass(frozen=True)
class DetachedBrowserLaunch:
    process: subprocess.Popen[bytes]
    log_path: Path


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
            chromium_executable = ensure_playwright_chromium_executable(
                self._playwright
            )
            _log(
                "Launching owned persistent browser context "
                f"(headless={self._config.headless}, profile={self._config.profile_dir}, "
                f"downloads={self._config.downloads_dir})"
            )
            self._browser_context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self._config.profile_dir),
                executable_path=str(chromium_executable),
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
            launch = self._launch_detached_browser(playwright)
            _wait_for_cdp_endpoint(
                self._config.browser_debug_url,
                timeout_seconds=CDP_LAUNCH_TIMEOUT_SECONDS,
                browser_process=launch.process,
                startup_log_path=launch.log_path,
            )
            browser = playwright.chromium.connect_over_cdp(
                self._config.browser_debug_url,
                timeout=CDP_CONNECT_TIMEOUT_MS,
            )
            _log(
                f"Attached to launched Chromium CDP endpoint: {self._config.browser_debug_url}"
            )
            return browser

    def _launch_detached_browser(self, playwright: Playwright) -> DetachedBrowserLaunch:
        if self._config.browser_debug_url is None:
            raise RuntimeError("Cannot launch detached browser without debug endpoint.")

        endpoint = _parse_local_debug_endpoint(self._config.browser_debug_url)
        chromium_executable = ensure_playwright_chromium_executable(playwright)
        removed_lock_paths = remove_stale_browser_profile_lock_files(
            self._config.profile_dir
        )
        if removed_lock_paths:
            removed_names = ", ".join(path.name for path in removed_lock_paths)
            _log(f"Removed stale Chromium profile lock file(s): {removed_names}")

        args = [
            str(chromium_executable),
            f"--remote-debugging-address={endpoint.host}",
            f"--remote-debugging-port={endpoint.port}",
            f"--user-data-dir={self._config.profile_dir}",
            *DETACHED_CHROMIUM_STARTUP_FLAGS,
            "about:blank",
        ]
        if self._config.headless:
            args.insert(1, "--headless=new")

        startup_log_path = self._config.profile_dir / "detached-chromium.log"
        with startup_log_path.open("wb") as startup_log:
            browser_process = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=startup_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        _log(
            "Detached Chromium launched "
            f"(pid={browser_process.pid}, profile={self._config.profile_dir}, "
            f"startup_log={startup_log_path})"
        )
        return DetachedBrowserLaunch(process=browser_process, log_path=startup_log_path)

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


def _wait_for_cdp_endpoint(
    endpoint_url: str,
    *,
    timeout_seconds: float,
    browser_process: subprocess.Popen[bytes] | None = None,
    startup_log_path: Path | None = None,
) -> None:
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
            if browser_process is not None and browser_process.poll() is not None:
                raise RuntimeError(
                    "Detached browser exited before debug endpoint was ready: "
                    f"{endpoint_url}. {_startup_log_summary(startup_log_path)}"
                ) from error
            time.sleep(0.2)

    raise RuntimeError(
        "Timed out waiting for detached browser debug endpoint: "
        f"{endpoint_url} ({last_error}). {_startup_log_summary(startup_log_path)}"
    )


def _startup_log_summary(startup_log_path: Path | None) -> str:
    if startup_log_path is None:
        return "No Chromium startup log was captured."

    try:
        startup_log = startup_log_path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        return f"Could not read Chromium startup log {startup_log_path}: {error}"

    log_lines = startup_log.strip().splitlines()
    if not log_lines:
        return f"Chromium startup log is empty: {startup_log_path}"

    return "Last Chromium startup log lines:\n" + "\n".join(log_lines[-20:])
