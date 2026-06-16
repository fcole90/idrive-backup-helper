from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
import time
from typing import Literal
from typing import Protocol
from typing import cast
from urllib.parse import parse_qsl
from urllib.parse import unquote
from urllib.parse import urlparse
from urllib.parse import urlunparse

from playwright.sync_api import (
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from idrive_backup_helper.browser.download_cache import (
    load_folder_entries_cache,
    write_folder_entries_cache,
)
from idrive_backup_helper.browser.download_entries import (
    ensure_raw_file_list,
    parse_remote_entries,
)
from idrive_backup_helper.browser.download_models import RemoteEntries, RemoteFile
from idrive_backup_helper.browser.session import ensure_authenticated_page

FOLDER_SETTLE_POLL_MS = 1_000
FOLDER_SETTLE_STABLE_TICKS = 10
FOLDER_LOADER_LOG_INTERVAL_SECONDS = 10
FOLDER_LOAD_RETRY_INTERVAL_MS = 10_000
FOLDER_LOAD_RETRY_TIMEOUT_MS = 120 * 60 * 1_000
DOWNLOAD_START_TIMEOUT_MS = 60_000
type SelectorState = Literal["attached", "detached", "hidden", "visible"]


def _log(message: str) -> None:
    print(f"[download-folder] {message}", flush=True)


class FolderViewPage(Protocol):
    def wait_for_selector(
        self,
        selector: str,
        *,
        state: SelectorState | None = None,
        timeout: float | None = None,
    ) -> object:
        pass

    def evaluate(self, expression: str) -> object:
        pass

    def wait_for_timeout(self, timeout: float) -> None:
        pass


def _js_asset_path(name: str) -> Path:
    return Path(__file__).resolve().parent / "js" / name


def _load_js_asset(name: str) -> str:
    asset_path = _js_asset_path(name)
    if not asset_path.exists():
        raise RuntimeError(f"Missing browser script asset: {asset_path}")

    return asset_path.read_text(encoding="utf-8")


def _ensure_trigger_result(raw_result: object, file_name: str) -> None:
    if not isinstance(raw_result, dict):
        return

    result_dict = cast(dict[object, object], raw_result)
    ok_value = result_dict.get("ok")
    if ok_value is True:
        return

    reason_value = result_dict.get("reason")
    if isinstance(reason_value, str) and reason_value:
        raise RuntimeError(reason_value)

    raise RuntimeError(f"Download trigger failed for {file_name}")


def _evaluate_current_folder_entries(page: Page) -> RemoteEntries:
    script = _load_js_asset("list_current_folder_files.js")
    raw_files: object = page.evaluate(
        script,
        {
            "scrollIntervalMs": 350,
            "maxIdleTicks": 3,
        },
    )
    return parse_remote_entries(ensure_raw_file_list(raw_files))


def _normalized_folder_url(url: str) -> str:
    parsed_url = urlparse(url)
    path = unquote(parsed_url.path).rstrip("/")
    query_pairs = sorted(parse_qsl(parsed_url.query, keep_blank_values=True))
    query = "&".join(f"{key}={value}" for key, value in query_pairs)
    return urlunparse(
        (
            parsed_url.scheme.lower(),
            parsed_url.netloc.lower(),
            path,
            "",
            query,
            "",
        )
    )


def is_current_folder_url(current_url: str, target_url: str) -> bool:
    if not current_url or current_url == "about:blank":
        return False

    return _normalized_folder_url(current_url) == _normalized_folder_url(target_url)


@dataclass(frozen=True)
class FolderViewState:
    loader_visible: bool
    content_row_count: int
    total_row_count: int


def _int_value(value: object | None) -> int:
    return value if isinstance(value, int) else 0


def _read_folder_view_state(page: FolderViewPage) -> FolderViewState:
    raw_state: object = page.evaluate("""
() => {
  const container = document.querySelector('#file_list_container');
  if (!container) {
    return { loaderVisible: false, contentRowCount: 0, totalRowCount: 0 };
  }

  const loader = container.querySelector('#loader, li.loader');
  const loaderStyle = loader ? window.getComputedStyle(loader) : null;
  const loaderVisible = Boolean(
    loader &&
    !loader.hidden &&
    loaderStyle &&
    loaderStyle.display !== 'none' &&
    loaderStyle.visibility !== 'hidden' &&
    loader.getClientRects().length > 0
  );

  const rows = [...container.querySelectorAll(':scope > li')];
  const contentRows = rows.filter(
    (row) => row.id !== 'loader' && !row.classList.contains('loader')
  );

  return {
    loaderVisible,
    contentRowCount: contentRows.length,
    totalRowCount: rows.length,
  };
}
""")
    if not isinstance(raw_state, Mapping):
        return FolderViewState(
            loader_visible=False,
            content_row_count=0,
            total_row_count=0,
        )

    return FolderViewState(
        loader_visible=raw_state.get("loaderVisible") is True,
        content_row_count=_int_value(raw_state.get("contentRowCount")),
        total_row_count=_int_value(raw_state.get("totalRowCount")),
    )


def wait_for_folder_view_settle(page: FolderViewPage, timeout_ms: int) -> None:
    page.wait_for_selector("#file_list_container", state="attached", timeout=timeout_ms)

    deadline = time.monotonic() + (timeout_ms / 1000)
    stable_ticks = 0
    last_content_row_count = -1
    next_loader_log_at = 0.0

    while time.monotonic() < deadline:
        view_state = _read_folder_view_state(page)
        now = time.monotonic()

        if view_state.loader_visible:
            stable_ticks = 0
            last_content_row_count = view_state.content_row_count
            if now >= next_loader_log_at:
                seconds_remaining = max(0, int(deadline - now))
                _log(
                    "Folder loader still visible; waiting "
                    f"({view_state.content_row_count} content row(s), "
                    f"{view_state.total_row_count} total row(s), "
                    f"{seconds_remaining}s remaining)"
                )
                next_loader_log_at = now + FOLDER_LOADER_LOG_INTERVAL_SECONDS
            page.wait_for_timeout(FOLDER_SETTLE_POLL_MS)
            continue

        if view_state.content_row_count == last_content_row_count:
            stable_ticks += 1
        else:
            stable_ticks = 0
            last_content_row_count = view_state.content_row_count

        if stable_ticks >= FOLDER_SETTLE_STABLE_TICKS:
            _log(
                "Folder view settled "
                f"({view_state.content_row_count} content row(s), "
                f"{view_state.total_row_count} total row(s))"
            )
            return

        page.wait_for_timeout(FOLDER_SETTLE_POLL_MS)

    raise RuntimeError("Timed out waiting for folder loader to finish")


def _read_breadcrumb_titles(page: Page) -> list[str]:
    raw_titles: object = page.evaluate("""
() => {
  const breadcrumb = document.querySelector('div.breadcrumb');
  if (!breadcrumb) {
    return [];
  }

  return [...breadcrumb.childNodes]
    .filter((node) => node.nodeType === 1)
    .map((node) => node.title || '')
    .filter((title) => title);
}
""")
    if not isinstance(raw_titles, list):
        return []

    typed_titles = cast(list[object], raw_titles)
    return [title for title in typed_titles if isinstance(title, str)]


def _ensure_expected_folder_loaded(
    page: Page, expected_folder_name: str | None
) -> None:
    if expected_folder_name is None:
        return

    breadcrumb_titles = _read_breadcrumb_titles(page)
    if expected_folder_name in breadcrumb_titles:
        return

    joined_titles = "/".join(breadcrumb_titles) if breadcrumb_titles else "<empty>"
    raise RuntimeError(
        "Loaded folder does not match expected path segment "
        f"'{expected_folder_name}'. Current breadcrumb: {joined_titles}"
    )


def _load_folder_with_retry(
    page: Page,
    *,
    target_url: str,
    timeout_ms: int,
    allow_interactive_login: bool,
    expected_folder_name: str | None,
) -> None:
    deadline = time.monotonic() + (FOLDER_LOAD_RETRY_TIMEOUT_MS / 1000)
    last_error: Exception = RuntimeError("Folder load retry exhausted")
    attempt = 1

    while True:
        try:
            _log(
                f"Loading folder attempt {attempt}: {target_url}"
                + (
                    f" (expecting '{expected_folder_name}')"
                    if expected_folder_name is not None
                    else ""
                )
            )
            if is_current_folder_url(page.url, target_url):
                _log(f"Current tab is already at target folder: {target_url}")
            else:
                _log(f"Navigating folder page from {page.url} to {target_url}")
                page.goto(target_url, wait_until="domcontentloaded")
            ensure_authenticated_page(
                page,
                target_url=target_url,
                allow_interactive_login=allow_interactive_login,
            )
            wait_for_folder_view_settle(page, timeout_ms)
            _ensure_expected_folder_loaded(page, expected_folder_name)
            _log(f"Folder load succeeded on attempt {attempt}: {target_url}")
            return
        except Exception as error:
            last_error = error
            _log(f"Folder load attempt {attempt} failed: {error}")

        if time.monotonic() >= deadline:
            break

        attempt += 1
        _log(
            f"Retrying folder load in {FOLDER_LOAD_RETRY_INTERVAL_MS // 1000}s: {target_url}"
        )
        page.wait_for_timeout(FOLDER_LOAD_RETRY_INTERVAL_MS)

    raise RuntimeError(
        "Failed to load folder after retries "
        f"({FOLDER_LOAD_RETRY_TIMEOUT_MS // 1000}s limit): {last_error}"
    )


def load_folder_entries_with_retry(
    page: Page,
    *,
    downloads_dir: Path,
    target_url: str,
    timeout_ms: int,
    allow_interactive_login: bool,
    expected_folder_name: str | None,
    use_folder_cache: bool,
) -> RemoteEntries:
    if use_folder_cache:
        cached_entries = load_folder_entries_cache(downloads_dir, target_url)
        if cached_entries is not None:
            if not cached_entries.files and not cached_entries.folders:
                _log(
                    "Ignoring empty cached folder entries and reloading: "
                    f"{target_url}"
                )
            else:
                _log(
                    "Using cached folder entries: "
                    f"{target_url} ({len(cached_entries.files)} file(s), "
                    f"{len(cached_entries.folders)} folder(s))"
                )
                return cached_entries

    deadline = time.monotonic() + (FOLDER_LOAD_RETRY_TIMEOUT_MS / 1000)
    last_error: Exception = RuntimeError("Folder entries retry exhausted")
    attempt = 1

    while True:
        try:
            _load_folder_with_retry(
                page,
                target_url=target_url,
                timeout_ms=timeout_ms,
                allow_interactive_login=allow_interactive_login,
                expected_folder_name=expected_folder_name,
            )
            entries = _evaluate_current_folder_entries(page)
            _log(
                f"Folder entries attempt {attempt}: found {len(entries.files)} file(s), "
                f"{len(entries.folders)} folder(s)"
            )
            if not entries.files and not entries.folders:
                _log("Folder entries are empty after loader settled")

            write_folder_entries_cache(downloads_dir, target_url, entries)

            return entries
        except Exception as error:
            last_error = error
            _log(f"Folder entries attempt {attempt} failed: {error}")

        if time.monotonic() >= deadline:
            break

        attempt += 1
        _log(
            f"Retrying folder entries in {FOLDER_LOAD_RETRY_INTERVAL_MS // 1000}s: {target_url}"
        )
        page.wait_for_timeout(FOLDER_LOAD_RETRY_INTERVAL_MS)

    raise RuntimeError(
        "Failed to extract folder entries after retries "
        f"({FOLDER_LOAD_RETRY_TIMEOUT_MS // 1000}s limit): {last_error}"
    )


def ensure_folder_loaded_for_download(
    page: Page,
    *,
    target_url: str,
    timeout_ms: int,
    allow_interactive_login: bool,
    expected_folder_name: str | None,
) -> None:
    _load_folder_with_retry(
        page,
        target_url=target_url,
        timeout_ms=timeout_ms,
        allow_interactive_login=allow_interactive_login,
        expected_folder_name=expected_folder_name,
    )


def download_one_file(
    page: Page,
    remote_file: RemoteFile,
    staging_dir: Path,
    cooldown_ms: int,
) -> Path:
    script = _load_js_asset("trigger_file_download.js")
    _log(f"Starting download: {remote_file.file_name}")

    try:
        with page.expect_download(timeout=DOWNLOAD_START_TIMEOUT_MS) as download_info:
            trigger_result: object = page.evaluate(
                script,
                {
                    "fileName": remote_file.file_name,
                    "rowIndex": remote_file.row_index,
                    "cooldownMs": cooldown_ms,
                },
            )
            _ensure_trigger_result(trigger_result, remote_file.file_name)

        download = download_info.value
    except PlaywrightTimeoutError as error:
        raise RuntimeError(
            "Timed out waiting for browser download to start: "
            f"{remote_file.file_name}. IDrive may still have a stale or blocked "
            "download in progress from a previous interrupted run."
        ) from error
    except PlaywrightError as error:
        raise RuntimeError(
            f"Download canceled by browser/session: {remote_file.file_name} ({error})"
        ) from error

    staged_path = staging_dir / download.suggested_filename
    try:
        download.save_as(str(staged_path))
    except PlaywrightError as error:
        raise RuntimeError(
            f"Failed saving download: {remote_file.file_name} ({error})"
        ) from error
    _log(f"Staged download complete: {remote_file.file_name} -> {staged_path}")
    return staged_path
