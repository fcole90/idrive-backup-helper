import json
import os
import socket
import subprocess
import time
from pathlib import Path
from dataclasses import dataclass
from types import TracebackType
from typing import cast
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import urlopen

import psutil
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
# A browser that is merely wedged or swapping can take many seconds to answer an
# HTTP probe, so one quick miss is not evidence that it died.
CDP_PROBE_TIMEOUT_SECONDS = 5.0
# Before concluding the browser process is gone (and killing/relaunching it), keep
# probing for this long: a hung-but-alive browser often starts answering again.
CDP_RECOVERY_WAIT_SECONDS = 60.0
CDP_RECOVERY_PROBE_INTERVAL_SECONDS = 2.0
BROWSER_TERMINATE_GRACE_SECONDS = 10.0
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
_PROC_GONE = (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess)


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
class BrowserHealthReport:
    """A read-only snapshot of the browser's liveness, taken after a mid-run death.

    Gathered without touching Playwright (only an HTTP probe of the CDP endpoint,
    a process scan, a subprocess ``poll``, and a log read), so it is safe to collect
    once the page or context is already gone. ``cdp_reachable`` alone cannot tell a
    dead browser from a hung one — a swamped machine can miss the probe while
    Chromium is still running — so it is read together with
    ``browser_processes_on_profile``, which counts the processes that actually exist.
    """

    mode: str
    cdp_url: str | None
    cdp_reachable: bool | None
    cdp_version: str | None
    detached_pid: int | None
    detached_exit_code: int | None
    detached_running: bool | None
    chromium_log_tail: str | None
    browser_processes_on_profile: int | None = None


def browser_processes_on_profile(profile_dir: Path) -> list[psutil.Process]:
    """Chromium processes running against our profile directory.

    Matched on the ``--user-data-dir`` switch, which is Chrome-specific, so this
    can never match our own Python process. The profile belongs to this tool, so
    whatever runs on it is ours to terminate regardless of who launched it.
    """
    marker = f"--user-data-dir={profile_dir}"
    matched: list[psutil.Process] = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = " ".join(proc.info.get("cmdline") or [])
        except _PROC_GONE:
            continue
        if marker in cmdline:
            matched.append(proc)
    return matched


def terminate_browser_processes_on_profile(profile_dir: Path) -> int:
    """Terminate (then kill) every browser process on our profile. Returns the count.

    Killing the roots normally takes the renderer children with them, but a wedged
    Chromium can leave orphans holding the profile lock, which would then block the
    relaunch — so the whole tree is collected up front and killed explicitly.
    """
    victims: dict[int, psutil.Process] = {}
    for root in browser_processes_on_profile(profile_dir):
        victims[root.pid] = root
        try:
            for child in root.children(recursive=True):
                victims[child.pid] = child
        except _PROC_GONE:
            continue

    processes = list(victims.values())
    if not processes:
        return 0

    for proc in processes:
        try:
            proc.terminate()
        except _PROC_GONE:
            continue

    _gone, alive = psutil.wait_procs(processes, timeout=BROWSER_TERMINATE_GRACE_SECONDS)
    for proc in alive:
        try:
            proc.kill()
        except _PROC_GONE:
            continue
    if alive:
        psutil.wait_procs(alive, timeout=BROWSER_TERMINATE_GRACE_SECONDS)

    return len(processes)


def _probe_cdp_version(
    endpoint_url: str, *, timeout_seconds: float = CDP_PROBE_TIMEOUT_SECONDS
) -> str | None:
    version_url = endpoint_url.rstrip("/") + "/json/version"
    try:
        with urlopen(version_url, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except OSError, URLError, TimeoutError:
        return None

    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip()[:200] or None

    if isinstance(parsed, dict):
        browser = cast(dict[object, object], parsed).get("Browser")
        if isinstance(browser, str) and browser:
            return browser
    return raw.strip()[:200] or None


@dataclass(frozen=True)
class BrowserConfig:
    profile_dir: Path
    staging_dir: Path
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
        self._detached_launch: DetachedBrowserLaunch | None = None

    def __enter__(self) -> "BrowserEngine":
        self._config.profile_dir.mkdir(parents=True, exist_ok=True)
        self._config.staging_dir.mkdir(parents=True, exist_ok=True)

        self._playwright = self._playwright_context.__enter__()
        self._browser_context = self._open_browser_context()
        self._browser_context.set_default_timeout(self._config.timeout_ms)
        return self

    def _open_browser_context(self) -> BrowserContext:
        playwright = self._playwright
        if playwright is None:
            raise RuntimeError(
                "BrowserEngine must be entered before opening a context."
            )

        if self._config.browser_debug_url is None:
            chromium_executable = ensure_playwright_chromium_executable(playwright)
            _log(
                "Launching owned persistent browser context "
                f"(headless={self._config.headless}, profile={self._config.profile_dir}, "
                f"downloads={self._config.staging_dir})"
            )
            return playwright.chromium.launch_persistent_context(
                user_data_dir=str(self._config.profile_dir),
                executable_path=str(chromium_executable),
                headless=self._config.headless,
                accept_downloads=True,
                downloads_path=str(self._config.staging_dir),
            )

        self._browser = self._connect_or_launch_browser(playwright)
        browser_context = self._default_browser_context(self._browser)
        _log(
            "Using detached browser context "
            f"with {len(browser_context.pages)} existing page(s)"
        )
        return browser_context

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

    def describe_browser_health(self) -> BrowserHealthReport:
        # Read-only: an HTTP probe, a subprocess poll, and a log read — no Playwright
        # calls — so this is safe to run after the page/context has already died.
        cdp_url = self._config.browser_debug_url
        launch = self._detached_launch

        cdp_reachable: bool | None = None
        cdp_version: str | None = None
        if cdp_url is not None:
            cdp_version = _probe_cdp_version(cdp_url)
            cdp_reachable = cdp_version is not None

        detached_pid = launch.process.pid if launch is not None else None
        detached_exit_code: int | None = None
        detached_running: bool | None = None
        if launch is not None:
            detached_exit_code = launch.process.poll()
            detached_running = detached_exit_code is None

        chromium_log_tail = (
            _startup_log_summary(launch.log_path) if launch is not None else None
        )

        try:
            profile_process_count = len(
                browser_processes_on_profile(self._config.profile_dir)
            )
        except Exception:
            profile_process_count = None

        if cdp_url is None:
            mode = "owned-context"
        elif launch is not None:
            mode = "launched-cdp"
        else:
            mode = "attached-cdp"

        return BrowserHealthReport(
            mode=mode,
            cdp_url=cdp_url,
            cdp_reachable=cdp_reachable,
            cdp_version=cdp_version,
            detached_pid=detached_pid,
            detached_exit_code=detached_exit_code,
            detached_running=detached_running,
            chromium_log_tail=chromium_log_tail,
            browser_processes_on_profile=profile_process_count,
        )

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

    def close_other_pages(self, keep: Page) -> int:
        """Close every tab except ``keep``, returning how many were closed.

        The profile is this tool's own, so any other tab is leftover state — a
        download-artifact error page, a wedged folder view — still holding a
        renderer. ``keep`` must already be open: dropping to zero tabs quits
        Chromium.
        """
        if self._browser_context is None:
            return 0

        closed_count = 0
        for tab in list(self._browser_context.pages):
            if tab is keep:
                continue
            try:
                if tab.is_closed():
                    continue
                tab.close()
            except Exception as error:
                _log(f"Could not close leftover tab: {error}")
                continue
            closed_count += 1

        if closed_count:
            _log(f"Closed {closed_count} leftover tab(s)")
        return closed_count

    def recover_page(self, dead_page: Page | None = None) -> Page:
        """Return a usable page after the current one died or stopped responding.

        Escalates only as far as it must:

        1. a fresh tab on the existing connection — enough when just the renderer
           died (OOM kill, Memory Saver discard) or a single tab wedged;
        2. a reconnect over CDP — the browser process is fine but our websocket to
           it dropped, which leaves every cached Playwright object permanently dead
           (``BrowserContext.new_page`` then fails exactly like the page did);
        3. a kill-and-relaunch of the browser itself.

        Raises ``RuntimeError`` if even a relaunched browser yields no page.
        """
        self._close_page_quietly(dead_page)

        page = self._new_page_or_none()
        if page is not None:
            _log("Recovered on the existing browser connection with a fresh tab")
            self.close_other_pages(keep=page)
            return page

        if self._config.browser_debug_url is not None:
            page = self._reattach_over_cdp_or_none()
            if page is not None:
                self.close_other_pages(keep=page)
                return page

        return self.restart_browser()

    def restart_browser(self) -> Page:
        """Kill every browser on our profile, launch a fresh one, and open a page.

        The profile survives the restart, so the IDrive session survives with it and
        the run resumes without a new login.
        """
        _log("Restarting the browser from scratch")
        self._discard_playwright_browser()

        terminated_count = terminate_browser_processes_on_profile(
            self._config.profile_dir
        )
        if terminated_count:
            _log(f"Terminated {terminated_count} browser process(es) on the profile")

        removed_lock_paths = remove_stale_browser_profile_lock_files(
            self._config.profile_dir
        )
        if removed_lock_paths:
            removed_names = ", ".join(path.name for path in removed_lock_paths)
            _log(f"Removed stale Chromium profile lock file(s): {removed_names}")

        browser_context = self._open_browser_context()
        browser_context.set_default_timeout(self._config.timeout_ms)
        self._browser_context = browser_context

        page = browser_context.new_page()
        _log("Browser relaunched; recovered onto a fresh page")
        self.close_other_pages(keep=page)
        return page

    def _new_page_or_none(self) -> Page | None:
        if self._browser_context is None:
            return None

        try:
            return self._browser_context.new_page()
        except Exception as error:
            _log(f"Could not open a tab on the existing connection: {error}")
            return None

    def _reattach_over_cdp_or_none(self) -> Page | None:
        endpoint_url = self._config.browser_debug_url
        playwright = self._playwright
        if endpoint_url is None or playwright is None:
            return None

        cdp_version = _wait_for_cdp_version(
            endpoint_url, timeout_seconds=CDP_RECOVERY_WAIT_SECONDS
        )
        if cdp_version is None:
            _log(
                "CDP endpoint stayed silent for "
                f"{CDP_RECOVERY_WAIT_SECONDS:.0f}s: {endpoint_url}"
            )
            return None

        _log(
            f"CDP endpoint still answers ({cdp_version}); the browser is alive and "
            "only our connection to it dropped. Reattaching."
        )
        self._discard_playwright_browser()
        try:
            browser = playwright.chromium.connect_over_cdp(
                endpoint_url,
                timeout=CDP_CONNECT_TIMEOUT_MS,
            )
            browser_context = self._default_browser_context(browser)
            browser_context.set_default_timeout(self._config.timeout_ms)
            page = browser_context.new_page()
        except (Error, RuntimeError) as error:
            _log(f"Reattaching over CDP failed: {error}")
            return None

        self._browser = browser
        self._browser_context = browser_context
        _log("Reattached to the running browser")
        return page

    def _discard_playwright_browser(self) -> None:
        # Drop the stale Playwright handles without calling close() on them: over a
        # CDP connection close() can reach through and shut the browser down, which
        # is the opposite of what recovery wants. A dead connection has nothing left
        # to release anyway, and a restart kills the process outright.
        self._browser = None
        self._browser_context = None

    @staticmethod
    def _close_page_quietly(page: Page | None) -> None:
        if page is None:
            return
        try:
            page.close()
        except Exception:
            pass  # Usually already gone; a close failure changes nothing.

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
            self._detached_launch = launch
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


def _wait_for_cdp_version(endpoint_url: str, *, timeout_seconds: float) -> str | None:
    # Keep asking rather than trusting one miss: under memory pressure the browser
    # can be too busy to answer for seconds at a time while still being perfectly
    # alive, and treating that as death would kill a browser worth keeping.
    deadline = time.monotonic() + timeout_seconds
    _log(f"Probing CDP endpoint for up to {timeout_seconds:.0f}s: {endpoint_url}")
    while True:
        cdp_version = _probe_cdp_version(endpoint_url)
        if cdp_version is not None:
            return cdp_version
        if time.monotonic() >= deadline:
            return None
        time.sleep(CDP_RECOVERY_PROBE_INTERVAL_SECONDS)


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
