from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright


@dataclass(frozen=True)
class BrowserConfig:
    profile_dir: Path
    downloads_dir: Path
    headless: bool
    timeout_ms: int


class BrowserEngine:
    def __init__(self, config: BrowserConfig) -> None:
        self._config = config
        self._playwright_context = sync_playwright()
        self._playwright: Playwright | None = None
        self._browser_context: BrowserContext | None = None

    def __enter__(self) -> "BrowserEngine":
        self._config.profile_dir.mkdir(parents=True, exist_ok=True)
        self._config.downloads_dir.mkdir(parents=True, exist_ok=True)

        self._playwright = self._playwright_context.__enter__()
        self._browser_context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self._config.profile_dir),
            headless=self._config.headless,
            accept_downloads=True,
            downloads_path=str(self._config.downloads_dir),
        )
        self._browser_context.set_default_timeout(self._config.timeout_ms)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._browser_context is not None:
            self._browser_context.close()

        self._playwright_context.__exit__(exc_type, exc, traceback)

    def new_page(self) -> Page:
        if self._browser_context is None:
            raise RuntimeError("BrowserEngine must be entered before creating a page.")

        return self._browser_context.new_page()
