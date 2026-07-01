from collections.abc import Iterator, Mapping
from dataclasses import dataclass
import hashlib
import html
from pathlib import Path
import random
import time
from typing import Literal
from typing import Protocol
from typing import cast
from urllib.parse import unquote
from urllib.parse import urlparse
from urllib.parse import urlunparse

from playwright.sync_api import (
    Download,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from idrive_backup_helper.browser.downloads.download_cache import (
    load_folder_entries_cache,
    write_folder_entries_cache,
)
from idrive_backup_helper.browser.downloads.folder_urls import (
    IDRIVE_HOME_PATH,
    IDRIVE_HOST_NAMES,
    is_idrive_url,
    normalized_folder_url,
)
from idrive_backup_helper.browser.downloads.download_entries import (
    ensure_raw_file_list,
    parse_remote_entries,
)
from idrive_backup_helper.browser.downloads.download_models import (
    RemoteEntries,
    RemoteFile,
    RemoteFolder,
)
from idrive_backup_helper.browser.session import ensure_authenticated_page

# Settle polling backs off instead of waiting a fixed ~10s per folder: check at
# ~1s, then 2/4/8s, then cap at 10s until the folder is ready. A fast folder
# settles in ~1s; only a genuinely slow load pays the longer waits.
FOLDER_SETTLE_BACKOFF_MS = (1_000, 2_000, 4_000, 8_000)
FOLDER_SETTLE_MAX_INTERVAL_MS = 10_000
# Human-ish +/- jitter applied to each interval so the cadence is not robotic.
FOLDER_SETTLE_JITTER_MS = 200
# An empty listing is ambiguous with "not started", so confirm across this many
# consecutive loader-gone, zero-row checks before treating the folder as empty.
FOLDER_SETTLE_EMPTY_CONFIRM_CHECKS = 2
FOLDER_LOADER_LOG_INTERVAL_SECONDS = 10
FOLDER_LOAD_RETRY_INTERVAL_MS = 10_000
FOLDER_LOAD_RETRY_TIMEOUT_MS = 120 * 60 * 1_000
DOWNLOAD_START_TIMEOUT_MS = 60_000
IDRIVE_NAVIGATION_BUILD_ID = "ui-click-prefix-v3"
FOLDER_CLICK_SETTLE_MIN_MS = 700
FOLDER_CLICK_SETTLE_MAX_MS = 1_800
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

    script = asset_path.read_text(encoding="utf-8")
    digest = hashlib.sha256(script.encode("utf-8")).hexdigest()[:12]
    _log(f"Loaded browser script asset: {asset_path.name} sha256={digest}")
    return script


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


def idrive_folder_path_parts(url: str) -> list[str]:
    if not is_idrive_url(url):
        return []

    parsed_url = urlparse(url)
    path_parts = [unquote(part) for part in parsed_url.path.split("/") if part]
    if len(path_parts) < 2 or path_parts[:2] != ["idrive", "home"]:
        raise RuntimeError(
            "IDrive folder URLs must start with /idrive/home, " f"got: {url}"
        )

    return path_parts[2:]


def _idrive_home_url(url: str) -> str:
    parsed_url = urlparse(url)
    return urlunparse(
        (
            parsed_url.scheme,
            parsed_url.netloc,
            IDRIVE_HOME_PATH,
            "",
            "",
            "",
        )
    )


def is_current_folder_url(current_url: str, target_url: str) -> bool:
    if not current_url or current_url == "about:blank":
        return False

    return normalized_folder_url(current_url) == normalized_folder_url(target_url)


def _human_delay_ms() -> int:
    return random.randint(FOLDER_CLICK_SETTLE_MIN_MS, FOLDER_CLICK_SETTLE_MAX_MS)


def _looks_like_idrive_device_id(value: str) -> bool:
    return len(value) >= 8 and value.isalnum() and any(char.isdigit() for char in value)


def _folder_click_name_candidates(path_part: str, path_index: int) -> list[str]:
    if path_index != 0:
        return [path_part]

    display_name, separator, suffix = path_part.rpartition("_")
    if not separator or not display_name or not _looks_like_idrive_device_id(suffix):
        return [path_part]

    return [display_name, path_part]


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


def _settle_backoff_intervals() -> Iterator[int]:
    yield from FOLDER_SETTLE_BACKOFF_MS
    while True:
        yield FOLDER_SETTLE_MAX_INTERVAL_MS


def _jittered(interval_ms: int) -> int:
    jitter = random.randint(-FOLDER_SETTLE_JITTER_MS, FOLDER_SETTLE_JITTER_MS)
    return max(FOLDER_SETTLE_JITTER_MS, interval_ms + jitter)


def wait_for_folder_view_settle(page: FolderViewPage, timeout_ms: int) -> None:
    page.wait_for_selector("#file_list_container", state="attached", timeout=timeout_ms)

    deadline = time.monotonic() + (timeout_ms / 1000)
    intervals = _settle_backoff_intervals()
    empty_checks = 0
    next_loader_log_at = 0.0

    # Wait before the first read so navigation has cleared the previous folder's
    # rows and shown the loader; checking at t=0 could otherwise settle on stale
    # content. A fast folder is then confirmed on that first ~1s check.
    while True:
        page.wait_for_timeout(_jittered(next(intervals)))
        view_state = _read_folder_view_state(page)
        now = time.monotonic()

        if view_state.loader_visible:
            empty_checks = 0
            if now >= next_loader_log_at:
                seconds_remaining = max(0, int(deadline - now))
                _log(
                    "Folder loader still visible; waiting "
                    f"({view_state.content_row_count} content row(s), "
                    f"{view_state.total_row_count} total row(s), "
                    f"{seconds_remaining}s remaining)"
                )
                next_loader_log_at = now + FOLDER_LOADER_LOG_INTERVAL_SECONDS
        elif view_state.content_row_count > 0:
            _log(
                "Folder view settled "
                f"({view_state.content_row_count} content row(s), "
                f"{view_state.total_row_count} total row(s))"
            )
            return
        else:
            empty_checks += 1
            if empty_checks >= FOLDER_SETTLE_EMPTY_CONFIRM_CHECKS:
                _log(
                    "Folder view settled empty after "
                    f"{empty_checks} check(s) (0 content row(s))"
                )
                return

        if time.monotonic() >= deadline:
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


def _ensure_click_result(raw_result: object, folder_name: str) -> None:
    if not isinstance(raw_result, dict):
        return

    result_dict = cast(dict[object, object], raw_result)
    ok_value = result_dict.get("ok")
    if ok_value is True:
        return

    reason_value = result_dict.get("reason")
    if isinstance(reason_value, str) and reason_value:
        raise RuntimeError(reason_value)

    raise RuntimeError(f"Folder click failed for {folder_name}")


def _decode_html_entities_in_href(href: str) -> str:
    # IDrive sometimes builds folder hrefs with HTML character references left in
    # the path (for example an apostrophe encoded as &#39; instead of %27). The
    # bare '#' then gets parsed as a URL fragment delimiter, truncating the path
    # (".../Quando scatta l&" instead of ".../Quando scatta l'allerta - ...") and
    # the folder click later fails to find the row. Decode the entities back to
    # their characters before any URL parsing so the full path survives.
    decoded = html.unescape(href)
    if decoded != href:
        _log(f"Decoded HTML entities in folder href: {href!r} -> {decoded!r}")
    return decoded


def normalize_folder_href(href: str) -> str:
    href = _decode_html_entities_in_href(href)
    parsed = urlparse(href)
    if parsed.hostname is None or parsed.hostname.lower() not in IDRIVE_HOST_NAMES:
        return href
    # Detect doubled /idrive/home prefix caused by relative href resolution against
    # the IDrive home page when the SPA navigates without updating window.location.
    if not unquote(parsed.path).startswith(IDRIVE_HOME_PATH * 2):
        return href
    fixed_path = parsed.path[len(IDRIVE_HOME_PATH) :]
    _log(f"Corrected doubled IDrive home prefix in href: {href!r}")
    return urlunparse((parsed.scheme, parsed.netloc, fixed_path, "", "", ""))


def normalize_remote_entries_hrefs(entries: RemoteEntries) -> RemoteEntries:
    fixed_folders = [
        RemoteFolder(
            folder_name=f.folder_name,
            href=normalize_folder_href(f.href),
        )
        for f in entries.folders
    ]
    if fixed_folders == list(entries.folders):
        return entries
    return RemoteEntries(files=entries.files, folders=fixed_folders)


def _ensure_idrive_click_path(target_url: str, path_parts: list[str]) -> None:
    if not is_idrive_url(target_url) or not path_parts:
        return

    if path_parts[:2] == ["idrive", "home"]:
        raise RuntimeError(
            "Resolved IDrive click path includes the fixed /idrive/home prefix: "
            f"{path_parts}. Refusing to click the wrong folder."
        )


type NavigationAction = Literal["click_down", "breadcrumb_up", "go_home"]


@dataclass(frozen=True)
class NavigationPlan:
    action: NavigationAction
    start_index: int


def plan_breadcrumb_navigation(
    current_titles: list[str], target_display_parts: list[str]
) -> NavigationPlan:
    """Decide how to reach ``target_display_parts`` from the current breadcrumb.

    The breadcrumb (not ``page.url``) is the source of truth for where we are, so
    sibling/cousin hops go up to the shared ancestor via one breadcrumb click
    instead of restarting from IDrive home and re-clicking the whole path.
    """
    if not target_display_parts:
        return NavigationPlan(action="click_down", start_index=0)

    root = target_display_parts[0]
    if root not in current_titles:
        # Different root, or no usable breadcrumb: restart from home.
        return NavigationPlan(action="go_home", start_index=0)

    # Drop any breadcrumb chrome before the target root (e.g. a leading "Home").
    aligned_current = current_titles[current_titles.index(root) :]
    common = 0
    for current_title, target_part in zip(aligned_current, target_display_parts):
        if current_title != target_part:
            break
        common += 1

    if common >= len(aligned_current):
        # Current folder is an ancestor of (or equal to) the target: click straight
        # down the remaining segments, no breadcrumb hop needed.
        return NavigationPlan(action="click_down", start_index=common)

    # Current folder is deeper than / diverges from the common ancestor: hop up to
    # it via the breadcrumb (ancestor is at path index common - 1), then click down.
    return NavigationPlan(action="breadcrumb_up", start_index=common)


def _click_breadcrumb(page: Page, candidates: list[str]) -> None:
    script = _load_js_asset("click_breadcrumb_by_name.js")
    result: object = page.evaluate(
        script,
        {
            "title": candidates[0],
            "titleCandidates": candidates,
            "settleMinMs": FOLDER_CLICK_SETTLE_MIN_MS,
            "settleMaxMs": FOLDER_CLICK_SETTLE_MAX_MS,
        },
    )
    _ensure_click_result(result, candidates[0])


def navigate_to_folder_with_clicks(
    page: Page, target_url: str, timeout_ms: int
) -> None:
    _log(
        "IDrive navigation implementation "
        f"{IDRIVE_NAVIGATION_BUILD_ID} from {Path(__file__).resolve()}"
    )
    path_parts = idrive_folder_path_parts(target_url)
    _ensure_idrive_click_path(target_url, path_parts)
    if not path_parts:
        _log(f"Navigating folder page directly: {target_url}")
        page.goto(target_url, wait_until="domcontentloaded")
        return

    target_display_parts = [
        _folder_click_name_candidates(path_part, index)[0]
        for index, path_part in enumerate(path_parts)
    ]
    if is_idrive_url(target_url):
        _log(f"Resolved IDrive click path: {' / '.join(target_display_parts)}")

    current_titles = _read_breadcrumb_titles(page)
    plan = plan_breadcrumb_navigation(current_titles, target_display_parts)
    start_index = plan.start_index

    if plan.action == "go_home":
        home_url = _idrive_home_url(target_url)
        if is_current_folder_url(page.url, home_url):
            _log(f"Current tab is already at IDrive home: {home_url}")
        else:
            _log(f"Navigating to IDrive home before folder clicks: {home_url}")
            page.goto(home_url, wait_until="domcontentloaded")
            page.wait_for_timeout(_human_delay_ms())
    elif plan.action == "breadcrumb_up":
        ancestor_index = start_index - 1
        ancestor_candidates = _folder_click_name_candidates(
            path_parts[ancestor_index], ancestor_index
        )
        _log(
            "Traversing up via breadcrumb to common ancestor: "
            f"{ancestor_candidates[0]}"
        )
        _click_breadcrumb(page, ancestor_candidates)
        page.wait_for_timeout(_human_delay_ms())
    elif start_index > 0:
        current_prefix = " / ".join(target_display_parts[:start_index])
        _log(f"Current tab is already at IDrive path prefix: {current_prefix}")

    click_folder_script = _load_js_asset("click_folder_by_name.js")
    for index, folder_name in enumerate(path_parts[start_index:], start=start_index):
        folder_name_candidates = _folder_click_name_candidates(folder_name, index)
        display_folder_name = folder_name_candidates[0]
        wait_for_folder_view_settle(page, timeout_ms)
        if display_folder_name == folder_name:
            _log(f"Opening folder via UI click: {folder_name}")
        else:
            _log(
                "Opening folder via UI click: "
                f"{display_folder_name} (URL segment: {folder_name})"
            )
        click_result: object = page.evaluate(
            click_folder_script,
            {
                "folderName": display_folder_name,
                "folderNameCandidates": folder_name_candidates,
                "settleMinMs": FOLDER_CLICK_SETTLE_MIN_MS,
                "settleMaxMs": FOLDER_CLICK_SETTLE_MAX_MS,
            },
        )
        _ensure_click_result(click_result, display_folder_name)
        page.wait_for_timeout(_human_delay_ms())


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
                _log(
                    f"Navigating folder page by UI clicks from {page.url} to {target_url}"
                )
                navigate_to_folder_with_clicks(page, target_url, timeout_ms)
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
        cached = load_folder_entries_cache(downloads_dir, target_url)
        if cached is not None:
            cached_entries = cached.entries
            if cached_entries.files or cached_entries.folders:
                cached_entries = normalize_remote_entries_hrefs(cached_entries)
                _log(
                    "Using cached folder entries: "
                    f"{target_url} ({len(cached_entries.files)} file(s), "
                    f"{len(cached_entries.folders)} folder(s))"
                )
                return cached_entries
            if cached.confirmed_empty:
                _log(f"Using cached confirmed-empty folder: {target_url}")
                return cached_entries
            _log(f"Ignoring untrusted empty cache and reloading: {target_url}")

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
            entries = normalize_remote_entries_hrefs(
                _evaluate_current_folder_entries(page)
            )
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

    failure = download.failure()
    if failure is not None:
        raise RuntimeError(f"Download failed: {remote_file.file_name} ({failure})")

    staged_path = _stage_download_on_volume(download, staging_dir, remote_file)
    _log(f"Staged download complete: {remote_file.file_name} -> {staged_path}")
    return staged_path


def _stage_download_on_volume(
    download: Download, staging_dir: Path, remote_file: RemoteFile
) -> Path:
    # For an owned local browser, Playwright already wrote the artifact into the
    # configured downloads_path (staging_dir, on the destination volume), so we
    # hand back that path directly and let the caller rename it into place: one
    # write, one antivirus scan, no copy. download.path() is unavailable over a
    # CDP connection, so there we fall back to streaming a copy onto the staging
    # volume; the subsequent rename to the final name is still same-volume.
    try:
        return Path(download.path())
    except PlaywrightError:
        staged_path = staging_dir / download.suggested_filename
        try:
            download.save_as(str(staged_path))
        except PlaywrightError as error:
            raise RuntimeError(
                f"Failed saving download: {remote_file.file_name} ({error})"
            ) from error
        return staged_path
