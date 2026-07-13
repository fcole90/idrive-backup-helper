from pathlib import Path
from typing import TypedDict
from typing import cast

import pytest
from playwright.sync_api import (
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from idrive_backup_helper.browser.downloads.download_models import (
    CachedFolderEntries,
    RemoteEntries,
    RemoteFile,
    RemoteFolder,
)
from idrive_backup_helper.browser.downloads.download_page import (
    DOWNLOAD_START_TIMEOUT_MS,
    FOLDER_LOAD_TIMEOUT_LIMIT,
    FOLDER_UNAVAILABLE_RETRY_LIMIT,
    RETRY_SLEEP_SLICE_SECONDS,
    BrowserClosedError,
    BrowserHungError,
    FolderLoadTimeoutError,
    FolderUnavailableError,
    NavigationPlan,
    SelectorState,
    plan_breadcrumb_navigation,
    normalize_folder_href,
    normalize_remote_entries_hrefs,
    download_one_file,
    ensure_folder_loaded_for_download,
    idrive_folder_path_parts,
    is_current_folder_url,
    load_folder_entries_with_retry,
    navigate_to_folder_with_clicks,
    wait_for_folder_view_settle,
)


class FolderClickPayload(TypedDict):
    folderName: str
    folderNameCandidates: list[str]
    settleMinMs: int
    settleMaxMs: int


class FakeFolderPage:
    def __init__(self, states: list[dict[str, object]]) -> None:
        self._states = states
        self._state_index = 0
        self.waited_timeouts: list[int] = []

    def wait_for_selector(
        self,
        selector: str,
        *,
        state: SelectorState | None = None,
        timeout: float | None = None,
    ) -> object:
        assert selector == "#file_list_container"
        assert state == "attached"
        assert timeout == 60_000
        return None

    def evaluate(self, expression: str) -> dict[str, object]:
        if self._state_index >= len(self._states):
            return self._states[-1]

        state = self._states[self._state_index]
        self._state_index += 1
        return state

    def wait_for_timeout(self, timeout: float) -> None:
        self.waited_timeouts.append(int(timeout))


def _identity_jitter(interval_ms: int) -> int:
    return interval_ms


def _patch_identity_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page._jittered",
        _identity_jitter,
    )


def test_wait_for_folder_view_settle_waits_for_loader_to_disappear(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_identity_jitter(monkeypatch)
    page = FakeFolderPage(
        [
            {"loaderVisible": True, "contentRowCount": 0, "totalRowCount": 1},
            {"loaderVisible": True, "contentRowCount": 2, "totalRowCount": 3},
            {"loaderVisible": False, "contentRowCount": 2, "totalRowCount": 2},
        ]
    )

    wait_for_folder_view_settle(page, timeout_ms=60_000)

    output = capsys.readouterr().out
    assert "Folder loader still visible" in output
    assert "Folder view settled (2 content row(s), 2 total row(s))" in output
    # Backoff cadence: settles on the third check, after ~1s, 2s, 4s waits.
    assert page.waited_timeouts == [1_000, 2_000, 4_000]


def test_wait_for_folder_view_settle_fast_folder_settles_on_first_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_identity_jitter(monkeypatch)
    page = FakeFolderPage(
        [{"loaderVisible": False, "contentRowCount": 1, "totalRowCount": 1}]
    )

    wait_for_folder_view_settle(page, timeout_ms=60_000)

    assert page.waited_timeouts == [1_000]


def test_wait_for_folder_view_settle_confirms_empty_after_two_checks(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_identity_jitter(monkeypatch)
    page = FakeFolderPage(
        [
            {"loaderVisible": False, "contentRowCount": 0, "totalRowCount": 0},
            {"loaderVisible": False, "contentRowCount": 0, "totalRowCount": 0},
        ]
    )

    wait_for_folder_view_settle(page, timeout_ms=60_000)

    output = capsys.readouterr().out
    assert "Folder view settled empty after 2 check(s)" in output
    assert page.waited_timeouts == [1_000, 2_000]


def test_wait_for_folder_view_settle_backoff_grows_and_caps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_identity_jitter(monkeypatch)
    page = FakeFolderPage(
        [
            *[
                {"loaderVisible": True, "contentRowCount": 0, "totalRowCount": 1}
                for _ in range(5)
            ],
            {"loaderVisible": False, "contentRowCount": 3, "totalRowCount": 3},
        ]
    )

    wait_for_folder_view_settle(page, timeout_ms=60_000)

    assert page.waited_timeouts == [1_000, 2_000, 4_000, 8_000, 10_000, 10_000]


def test_load_folder_entries_accepts_settled_empty_folder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    load_calls = 0

    def fake_load_folder_with_retry(*args: object, **kwargs: object) -> None:
        nonlocal load_calls
        load_calls += 1

    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.load_folder_entries_cache",
        _fake_load_folder_entries_cache,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page._load_folder_with_retry",
        fake_load_folder_with_retry,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page._evaluate_current_folder_entries",
        _fake_evaluate_current_folder_entries,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.write_folder_entries_cache",
        _fake_write_folder_entries_cache,
    )
    page_stub = cast(Page, object())

    entries = load_folder_entries_with_retry(
        page_stub,
        downloads_dir=tmp_path,
        target_url="https://example.com/folder",
        timeout_ms=60_000,
        allow_interactive_login=True,
        expected_folder_name=None,
        use_folder_cache=True,
    )

    assert entries.files == []
    assert entries.folders == []
    assert load_calls == 1


def test_load_folder_entries_trusts_confirmed_empty_cache_without_reload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    load_calls = 0

    def fake_load_folder_with_retry(*args: object, **kwargs: object) -> None:
        nonlocal load_calls
        load_calls += 1

    def fake_cache(downloads_dir: Path, target_url: str) -> CachedFolderEntries:
        return CachedFolderEntries(
            entries=RemoteEntries(files=[], folders=[]),
            confirmed_empty=True,
        )

    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.load_folder_entries_cache",
        fake_cache,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page._load_folder_with_retry",
        fake_load_folder_with_retry,
    )
    page_stub = cast(Page, object())

    entries = load_folder_entries_with_retry(
        page_stub,
        downloads_dir=tmp_path,
        target_url="https://example.com/folder",
        timeout_ms=60_000,
        allow_interactive_login=True,
        expected_folder_name=None,
        use_folder_cache=True,
    )

    assert entries.files == []
    assert entries.folders == []
    assert load_calls == 0


def test_load_folder_entries_reloads_untrusted_empty_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    load_calls = 0

    def fake_load_folder_with_retry(*args: object, **kwargs: object) -> None:
        nonlocal load_calls
        load_calls += 1

    def fake_cache(downloads_dir: Path, target_url: str) -> CachedFolderEntries:
        return CachedFolderEntries(
            entries=RemoteEntries(files=[], folders=[]),
            confirmed_empty=False,
        )

    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.load_folder_entries_cache",
        fake_cache,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page._load_folder_with_retry",
        fake_load_folder_with_retry,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page._evaluate_current_folder_entries",
        _fake_evaluate_current_folder_entries,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.write_folder_entries_cache",
        _fake_write_folder_entries_cache,
    )
    page_stub = cast(Page, object())

    entries = load_folder_entries_with_retry(
        page_stub,
        downloads_dir=tmp_path,
        target_url="https://example.com/folder",
        timeout_ms=60_000,
        allow_interactive_login=True,
        expected_folder_name=None,
        use_folder_cache=True,
    )

    assert entries.files == []
    assert entries.folders == []
    assert load_calls == 1


def test_load_folder_entries_reuses_tab_when_current_url_matches_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    page = FakeLoadPage(
        url="https://www.idrive.com/idrive/home/device/F/my%20path/fold_2/"
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.ensure_authenticated_page",
        _fake_ensure_authenticated_page,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.wait_for_folder_view_settle",
        _fake_wait_for_folder_view_settle,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page._evaluate_current_folder_entries",
        _fake_evaluate_current_folder_entries,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.write_folder_entries_cache",
        _fake_write_folder_entries_cache,
    )

    entries = load_folder_entries_with_retry(
        cast(Page, page),
        downloads_dir=tmp_path,
        target_url="https://www.idrive.com/idrive/home/device/F/my path/fold_2",
        timeout_ms=60_000,
        allow_interactive_login=True,
        expected_folder_name=None,
        use_folder_cache=False,
    )

    assert entries.files == []
    assert entries.folders == []
    assert page.navigated_urls == []


def test_load_folder_entries_reuses_tab_when_idrive_query_differs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    page = FakeLoadPage(
        url="https://www.idrive.com/idrive/home/device/F/BACKUP%202/?cache=123"
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.ensure_authenticated_page",
        _fake_ensure_authenticated_page,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.wait_for_folder_view_settle",
        _fake_wait_for_folder_view_settle,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page._evaluate_current_folder_entries",
        _fake_evaluate_current_folder_entries,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.write_folder_entries_cache",
        _fake_write_folder_entries_cache,
    )

    load_folder_entries_with_retry(
        cast(Page, page),
        downloads_dir=tmp_path,
        target_url="https://www.idrive.com/idrive/home/device/F/BACKUP 2/",
        timeout_ms=60_000,
        allow_interactive_login=True,
        expected_folder_name=None,
        use_folder_cache=False,
    )

    assert page.navigated_urls == []


def test_load_folder_entries_navigates_when_current_url_differs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    page = FakeLoadPage(url="https://www.idrive.com/idrive/home/device/F/fold_1")
    clicked_urls: list[str] = []
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.ensure_authenticated_page",
        _fake_ensure_authenticated_page,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.wait_for_folder_view_settle",
        _fake_wait_for_folder_view_settle,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page._evaluate_current_folder_entries",
        _fake_evaluate_current_folder_entries,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.write_folder_entries_cache",
        _fake_write_folder_entries_cache,
    )

    def fake_navigate_to_folder_with_clicks(
        page: FakeLoadPage,
        target_url: str,
        timeout_ms: int,
    ) -> None:
        clicked_urls.append(target_url)
        page.url = target_url

    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.navigate_to_folder_with_clicks",
        fake_navigate_to_folder_with_clicks,
    )

    target_url = "https://www.idrive.com/idrive/home/device/F/fold_2"
    load_folder_entries_with_retry(
        cast(Page, page),
        downloads_dir=tmp_path,
        target_url=target_url,
        timeout_ms=60_000,
        allow_interactive_login=True,
        expected_folder_name=None,
        use_folder_cache=False,
    )

    assert clicked_urls == [target_url]
    assert page.navigated_urls == []


def test_load_folder_entries_retry_sleep_is_chunked_while_open(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # The 10s backoff is slept as 1s slices, not one blocking wait, so the
    # cooperatively-scheduled Playwright loop is never frozen for the whole wait.
    attempts = {"count": 0}

    def fake_load_folder_with_retry(*args: object, **kwargs: object) -> None:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise PlaywrightError("transient folder load failure")

    slept: list[float] = []
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page._load_folder_with_retry",
        fake_load_folder_with_retry,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page._evaluate_current_folder_entries",
        _fake_evaluate_current_folder_entries,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.write_folder_entries_cache",
        _fake_write_folder_entries_cache,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.time.sleep",
        slept.append,
    )

    load_folder_entries_with_retry(
        cast(Page, FakeOpenPage()),
        downloads_dir=tmp_path,
        target_url="https://www.idrive.com/idrive/home/device/F/folder",
        timeout_ms=60_000,
        allow_interactive_login=True,
        expected_folder_name=None,
        use_folder_cache=False,
    )

    assert slept == [RETRY_SLEEP_SLICE_SECONDS] * 10


def test_load_folder_entries_retry_sleep_stops_early_when_page_closes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # The page is alive at the post-failure abort check but closes during the
    # backoff: the sliced sleep bails after one slice, then the next attempt's
    # abort check turns the closed page into a clean BrowserClosedError.
    def fake_load_folder_with_retry(*args: object, **kwargs: object) -> None:
        raise PlaywrightError("Target page, context or browser has been closed")

    slept: list[float] = []
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page._load_folder_with_retry",
        fake_load_folder_with_retry,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.time.sleep",
        slept.append,
    )

    with pytest.raises(BrowserClosedError):
        load_folder_entries_with_retry(
            cast(Page, FakeClosesDuringSleepPage()),
            downloads_dir=tmp_path,
            target_url="https://www.idrive.com/idrive/home/device/F/folder",
            timeout_ms=60_000,
            allow_interactive_login=True,
            expected_folder_name=None,
            use_folder_cache=False,
        )

    assert slept == [RETRY_SLEEP_SLICE_SECONDS]


def test_load_folder_entries_aborts_without_retry_when_browser_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # A closed page never recovers: abort with a clear error instead of throwing
    # a raw TargetClosedError from the retry sleep or looping for the full window.
    def fake_load_folder_with_retry(*args: object, **kwargs: object) -> None:
        raise PlaywrightError("Target page, context or browser has been closed")

    def boom_sleep(_seconds: float) -> None:
        raise AssertionError("must not sleep-retry a closed browser")

    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page._load_folder_with_retry",
        fake_load_folder_with_retry,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.time.sleep",
        boom_sleep,
    )

    with pytest.raises(RuntimeError, match="Browser was closed mid-run"):
        load_folder_entries_with_retry(
            cast(Page, FakeClosedPage()),
            downloads_dir=tmp_path,
            target_url="https://www.idrive.com/idrive/home/device/F/folder",
            timeout_ms=60_000,
            allow_interactive_login=True,
            expected_folder_name=None,
            use_folder_cache=False,
        )


def test_navigate_to_folder_with_clicks_raises_on_idrive_error_box(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # When the folder click surfaces IDrive's "There is some problem" box, the
    # click helper reports it and navigation bails with FolderUnavailableError
    # rather than pretending the click succeeded.
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.wait_for_folder_view_settle",
        _fake_wait_for_folder_view_settle,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page._load_js_asset",
        _fake_load_js_asset,
    )
    page = FakeUnavailableClickPage(url="https://www.idrive.com/idrive/home")

    with pytest.raises(FolderUnavailableError, match="refused to open"):
        navigate_to_folder_with_clicks(
            cast(Page, page),
            "https://www.idrive.com/idrive/home/device/F/fold_2",
            60_000,
        )

    # Bailed on the very first failing click, not after clicking deeper.
    assert page.folder_click_calls == 1


def test_load_folder_entries_quick_retries_then_skips_unavailable_folder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # A folder IDrive refuses to open is retried only a few times with a short
    # pause and then surfaced as FolderUnavailableError — never looping the full
    # multi-hour folder-load window.
    navigate_calls = {"count": 0}

    def fake_navigate(page: object, target_url: str, timeout_ms: int) -> None:
        navigate_calls["count"] += 1
        raise FolderUnavailableError(
            'IDrive refused to open folder "folder": '
            "There is some problem. Try later."
        )

    slept: list[float] = []
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.ensure_authenticated_page",
        _fake_ensure_authenticated_page,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.wait_for_folder_view_settle",
        _fake_wait_for_folder_view_settle,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.navigate_to_folder_with_clicks",
        fake_navigate,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.time.sleep",
        slept.append,
    )

    with pytest.raises(FolderUnavailableError):
        load_folder_entries_with_retry(
            cast(
                Page, FakeUnavailableLoadPage(url="https://www.idrive.com/idrive/home")
            ),
            downloads_dir=tmp_path,
            target_url="https://www.idrive.com/idrive/home/device/F/folder",
            timeout_ms=60_000,
            allow_interactive_login=True,
            expected_folder_name=None,
            use_folder_cache=False,
        )

    # Exactly the quick-retry budget of navigation attempts, no more.
    assert navigate_calls["count"] == FOLDER_UNAVAILABLE_RETRY_LIMIT
    # Each of the (limit - 1) short backoffs (3s) is slept as 1s slices; the long
    # 10s folder-load backoff is never used for an unavailable folder.
    assert slept == [RETRY_SLEEP_SLICE_SECONDS] * (
        (FOLDER_UNAVAILABLE_RETRY_LIMIT - 1) * 3
    )


class FakeBannerLoadPage:
    """Open page that stays on the parent after a click and serves breadcrumb
    titles plus error-banner text for the two inline reads in
    `_ensure_expected_folder_loaded` (branching on the evaluated script)."""

    def __init__(
        self, *, url: str, breadcrumb_titles: list[str], banner_text: str | None
    ) -> None:
        self.url = url
        self._titles = breadcrumb_titles
        self._banner_text = banner_text

    def is_closed(self) -> bool:
        return False

    def wait_for_timeout(self, timeout: float) -> None:
        pass

    def evaluate(self, expression: str) -> object:
        if "error_msg" in expression:
            return self._banner_text
        return list(self._titles)


def test_ensure_folder_loaded_skips_folder_when_error_banner_shows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The click "succeeded" (the click script's short poll missed the banner) but
    # the SPA stayed on the parent: the breadcrumb never reaches the target and the
    # ~10s banner is still up. This is IDrive refusing the folder, so we skip after
    # the quick-retry budget instead of hanging on the full load window.
    navigate_calls = {"count": 0}

    def fake_navigate(page: object, target_url: str, timeout_ms: int) -> None:
        navigate_calls["count"] += 1

    def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.ensure_authenticated_page",
        _fake_ensure_authenticated_page,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.wait_for_folder_view_settle",
        _fake_wait_for_folder_view_settle,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.navigate_to_folder_with_clicks",
        fake_navigate,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.time.sleep",
        no_sleep,
    )

    page = FakeBannerLoadPage(
        url="https://www.idrive.com/idrive/home/device/F",
        breadcrumb_titles=["device", "F"],
        banner_text="There is some problem. Try later.",
    )

    with pytest.raises(FolderUnavailableError, match="refused to open"):
        ensure_folder_loaded_for_download(
            cast(Page, page),
            target_url="https://www.idrive.com/idrive/home/device/F/broken_folder",
            timeout_ms=60_000,
            allow_interactive_login=False,
            expected_folder_name="broken_folder",
        )

    # Only the quick-retry budget of attempts, not the full load-retry window.
    assert navigate_calls["count"] == FOLDER_UNAVAILABLE_RETRY_LIMIT


class FakeLateBannerPage:
    """Breadcrumb never reaches the target; the error banner only becomes visible
    after a few reads, so a single check would miss it but the poll catches it."""

    def __init__(
        self, *, breadcrumb_titles: list[str], banner_text: str, banner_after_reads: int
    ) -> None:
        self.url = "https://www.idrive.com/idrive/home/device/F"
        self._titles = breadcrumb_titles
        self._banner_text = banner_text
        self._banner_after_reads = banner_after_reads
        self._banner_reads = 0
        self.wait_calls = 0

    def is_closed(self) -> bool:
        return False

    def wait_for_timeout(self, timeout: float) -> None:
        self.wait_calls += 1

    def evaluate(self, expression: str) -> object:
        if "error_msg" in expression:
            self._banner_reads += 1
            if self._banner_reads > self._banner_after_reads:
                return self._banner_text
            return None
        return list(self._titles)


class FakeLateLoadPage:
    """Breadcrumb starts on the parent and only reaches the target after a few
    reads (a slightly late load); no banner ever shows."""

    def __init__(
        self, *, parent_titles: list[str], target_title: str, load_after_reads: int
    ) -> None:
        self.url = "https://www.idrive.com/idrive/home/device/F"
        self._parent_titles = parent_titles
        self._target_title = target_title
        self._load_after_reads = load_after_reads
        self._breadcrumb_reads = 0

    def is_closed(self) -> bool:
        return False

    def wait_for_timeout(self, timeout: float) -> None:
        pass

    def evaluate(self, expression: str) -> object:
        if "error_msg" in expression:
            return None
        self._breadcrumb_reads += 1
        if self._breadcrumb_reads > self._load_after_reads:
            return [*self._parent_titles, self._target_title]
        return list(self._parent_titles)


def test_ensure_folder_loaded_catches_late_rendering_error_banner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The banner renders a beat after the click script's poll already returned and
    # after the first breadcrumb/banner check: the outcome poll keeps looking and
    # still classifies the folder as unavailable rather than an ordinary load miss.
    def fake_navigate(page: object, target_url: str, timeout_ms: int) -> None:
        return None

    def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.ensure_authenticated_page",
        _fake_ensure_authenticated_page,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.wait_for_folder_view_settle",
        _fake_wait_for_folder_view_settle,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.navigate_to_folder_with_clicks",
        fake_navigate,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.time.sleep",
        no_sleep,
    )

    page = FakeLateBannerPage(
        breadcrumb_titles=["device", "F"],
        banner_text="There is some problem. Try later.",
        banner_after_reads=2,
    )

    with pytest.raises(FolderUnavailableError, match="refused to open"):
        ensure_folder_loaded_for_download(
            cast(Page, page),
            target_url="https://www.idrive.com/idrive/home/device/F/broken_folder",
            timeout_ms=60_000,
            allow_interactive_login=False,
            expected_folder_name="broken_folder",
        )

    # It polled (waited) before the banner became visible instead of failing at once.
    assert page.wait_calls >= 2


def test_ensure_folder_loaded_accepts_late_breadcrumb_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The folder loads a beat late (breadcrumb reaches the target after a couple of
    # reads): the poll rescues it, so a slow-but-valid folder is not falsely skipped.
    navigate_calls = {"count": 0}

    def fake_navigate(page: object, target_url: str, timeout_ms: int) -> None:
        navigate_calls["count"] += 1

    def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.ensure_authenticated_page",
        _fake_ensure_authenticated_page,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.wait_for_folder_view_settle",
        _fake_wait_for_folder_view_settle,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.navigate_to_folder_with_clicks",
        fake_navigate,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.time.sleep",
        no_sleep,
    )

    page = FakeLateLoadPage(
        parent_titles=["device", "F"],
        target_title="slow_folder",
        load_after_reads=2,
    )

    ensure_folder_loaded_for_download(
        cast(Page, page),
        target_url="https://www.idrive.com/idrive/home/device/F/slow_folder",
        timeout_ms=60_000,
        allow_interactive_login=False,
        expected_folder_name="slow_folder",
    )

    # Loaded on the first attempt — no quick-retry skip was triggered.
    assert navigate_calls["count"] == 1


def test_is_current_folder_url_normalizes_encoding_and_trailing_slash() -> None:
    assert (
        is_current_folder_url(
            "https://www.idrive.com/idrive/home/device/F/my%20path/fold_2/",
            "https://www.idrive.com/idrive/home/device/F/my path/fold_2",
        )
        is True
    )


def test_is_current_folder_url_preserves_non_idrive_query_params() -> None:
    assert (
        is_current_folder_url(
            "https://example.com/idrive/home/device?cache=123",
            "https://example.com/idrive/home/device",
        )
        is False
    )


def test_normalize_folder_href_fixes_doubled_idrive_home_prefix() -> None:
    assert (
        normalize_folder_href(
            "https://www.idrive.com/idrive/home/idrive/home/DEVICE/drive/folder"
        )
        == "https://www.idrive.com/idrive/home/DEVICE/drive/folder"
    )


def test_normalize_folder_href_leaves_correct_idrive_url_unchanged() -> None:
    url = "https://www.idrive.com/idrive/home/DEVICE/drive/folder"
    assert normalize_folder_href(url) == url


def test_normalize_folder_href_leaves_non_idrive_url_unchanged() -> None:
    url = "https://example.com/idrive/home/idrive/home/DEVICE"
    assert normalize_folder_href(url) == url


def test_normalize_folder_href_decodes_html_entity_apostrophe() -> None:
    # IDrive leaves an HTML &#39; in the path; the bare '#' would otherwise be
    # parsed as a fragment and truncate the folder name to "Quando scatta l&".
    normalized = normalize_folder_href(
        "https://www.idrive.com/idrive/home/path/to/"
        "Quando%20scatta%20l&#39;allerta%20-%20Scienza%26Tecnica_files"
    )
    assert idrive_folder_path_parts(normalized) == [
        "path",
        "to",
        "Quando scatta l'allerta - Scienza&Tecnica_files",
    ]


def test_normalize_remote_entries_hrefs_fixes_doubled_subfolder_hrefs() -> None:
    entries = RemoteEntries(
        files=[
            RemoteFile(
                file_name="a.txt",
                row_index=0,
                server_size_text=None,
                server_modified_text=None,
            )
        ],
        folders=[
            RemoteFolder(
                folder_name="DEVICE",
                href="https://www.idrive.com/idrive/home/idrive/home/DEVICE/drive/folder",
            )
        ],
    )
    normalized = normalize_remote_entries_hrefs(entries)
    assert (
        normalized.folders[0].href
        == "https://www.idrive.com/idrive/home/DEVICE/drive/folder"
    )
    assert normalized.files == entries.files


def test_normalize_remote_entries_hrefs_returns_same_object_when_no_fix_needed() -> (
    None
):
    entries = RemoteEntries(
        files=[],
        folders=[
            RemoteFolder(
                folder_name="DEVICE",
                href="https://www.idrive.com/idrive/home/DEVICE/drive/folder",
            )
        ],
    )
    assert normalize_remote_entries_hrefs(entries) is entries


def test_idrive_folder_path_parts_decodes_target_path() -> None:
    assert idrive_folder_path_parts(
        "https://www.idrive.com/idrive/home/device/F/BACKUP%202/recup_dir.3/"
    ) == ["device", "F", "BACKUP 2", "recup_dir.3"]


def test_idrive_folder_path_parts_accepts_home_without_click_parts() -> None:
    assert idrive_folder_path_parts("https://www.idrive.com/idrive/home") == []


def test_idrive_folder_path_parts_rejects_idrive_url_without_home_prefix() -> None:
    with pytest.raises(RuntimeError, match="/idrive/home"):
        idrive_folder_path_parts("https://www.idrive.com/prefix/idrive/home/device")


def test_idrive_folder_path_parts_ignores_non_idrive_urls() -> None:
    assert idrive_folder_path_parts("https://example.com/idrive/home/device") == []


def test_navigate_to_folder_with_clicks_falls_back_for_non_idrive_url() -> None:
    page = FakeLoadPage(url="about:blank")

    navigate_to_folder_with_clicks(
        cast(Page, page), "https://example.com/folder", 60_000
    )

    assert page.navigated_urls == ["https://example.com/folder"]


def test_navigate_to_folder_with_clicks_rejects_leaked_idrive_home_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_idrive_folder_path_parts(target_url: str) -> list[str]:
        return ["idrive", "home", "device"]

    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.idrive_folder_path_parts",
        fake_idrive_folder_path_parts,
    )

    with pytest.raises(RuntimeError, match="/idrive/home prefix"):
        navigate_to_folder_with_clicks(
            cast(Page, FakeLoadPage(url="about:blank")),
            "https://www.idrive.com/idrive/home/device",
            60_000,
        )


def test_navigate_to_folder_with_clicks_uses_device_display_name_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.wait_for_folder_view_settle",
        _fake_wait_for_folder_view_settle,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page._load_js_asset",
        _fake_load_js_asset,
    )
    page = FakeClickPage(url="https://www.idrive.com/idrive/home")

    navigate_to_folder_with_clicks(
        cast(Page, page),
        "https://www.idrive.com/idrive/home/DESKTOP-CUVQN6N_D01780852158000245689/F/BACKUP%202/",
        60_000,
    )

    assert page.navigated_urls == []
    assert page.evaluate_payloads == [
        {
            "folderName": "DESKTOP-CUVQN6N",
            "folderNameCandidates": [
                "DESKTOP-CUVQN6N",
                "DESKTOP-CUVQN6N_D01780852158000245689",
            ],
            "settleMinMs": 700,
            "settleMaxMs": 1800,
        },
        {
            "folderName": "F",
            "folderNameCandidates": ["F"],
            "settleMinMs": 700,
            "settleMaxMs": 1800,
        },
        {
            "folderName": "BACKUP 2",
            "folderNameCandidates": ["BACKUP 2"],
            "settleMinMs": 700,
            "settleMaxMs": 1800,
        },
    ]


def test_navigate_to_folder_with_clicks_starts_after_current_idrive_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.wait_for_folder_view_settle",
        _fake_wait_for_folder_view_settle,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page._load_js_asset",
        _fake_load_js_asset,
    )
    page = FakeClickPage(
        url="https://www.idrive.com/idrive/home/device/F",
        breadcrumb_address_indexes=[0, 1],
    )

    navigate_to_folder_with_clicks(
        cast(Page, page),
        "https://www.idrive.com/idrive/home/device/F/path/to/destination",
        60_000,
    )

    assert page.navigated_urls == []
    assert page.breadcrumb_clicks == []
    assert [payload["folderName"] for payload in page.evaluate_payloads] == [
        "path",
        "to",
        "destination",
    ]


def test_navigate_to_folder_with_clicks_hops_up_to_common_ancestor_via_breadcrumb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.wait_for_folder_view_settle",
        _fake_wait_for_folder_view_settle,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page._load_js_asset",
        _fake_load_js_asset,
    )
    page = FakeClickPage(
        url="https://www.idrive.com/idrive/home/device/F/other",
        breadcrumb_address_indexes=[0, 1, 2],
    )

    navigate_to_folder_with_clicks(
        cast(Page, page),
        "https://www.idrive.com/idrive/home/device/F/path/to/destination",
        60_000,
    )

    # Sibling hop: up to the shared "F" (addressindex 1) via breadcrumb, then down
    # the rest. No home restart.
    assert page.navigated_urls == []
    assert [click["addressIndex"] for click in page.breadcrumb_clicks] == [1]
    assert [payload["folderName"] for payload in page.evaluate_payloads] == [
        "path",
        "to",
        "destination",
    ]


def test_navigate_to_folder_with_clicks_restarts_from_home_for_different_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.wait_for_folder_view_settle",
        _fake_wait_for_folder_view_settle,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page._load_js_asset",
        _fake_load_js_asset,
    )
    # Current breadcrumb is under a different device root, so there is no shared
    # ancestor to hop to: fall back to home and click the whole path down.
    page = FakeClickPage(
        url="https://www.idrive.com/idrive/home/otherdevice/X",
        breadcrumb_address_indexes=[0, 1],
    )

    navigate_to_folder_with_clicks(
        cast(Page, page),
        "https://www.idrive.com/idrive/home/device/F/path/to/destination",
        60_000,
    )

    assert page.navigated_urls == ["https://www.idrive.com/idrive/home"]
    assert page.breadcrumb_clicks == []
    assert [payload["folderName"] for payload in page.evaluate_payloads] == [
        "device",
        "F",
        "path",
        "to",
        "destination",
    ]


def test_navigate_to_folder_with_clicks_climbs_then_hops_when_collapsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.wait_for_folder_view_settle",
        _fake_wait_for_folder_view_settle,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page._load_js_asset",
        _fake_load_js_asset,
    )
    # At device/DRIVE/A/B/C/D/E, but the breadcrumb only exposes the three deepest
    # crumbs (C=4, D=5, E=6) — the shared ancestor A is collapsed. Target diverges
    # at A vs Bx. Climb to the shallowest visible crumb (C=4); from device/DRIVE/A/B/C
    # the breadcrumb re-renders the full leading path, so the re-plan can hop up to A.
    page = FakeClimbPage(
        url="https://www.idrive.com/idrive/home/device/DRIVE/A/B/C/D/E",
        breadcrumb_address_indexes=[4, 5, 6],
        on_breadcrumb_click={
            4: (
                "https://www.idrive.com/idrive/home/device/DRIVE/A/B/C",
                [0, 1, 2, 3, 4],
            ),
        },
    )

    navigate_to_folder_with_clicks(
        cast(Page, page),
        "https://www.idrive.com/idrive/home/device/DRIVE/A/Bx/Y",
        60_000,
    )

    # One climb (to C=4) then one hop (to A=2), no home restart, then descend.
    assert page.navigated_urls == []
    assert page.breadcrumb_clicks == [4, 2]
    assert [payload["folderName"] for payload in page.evaluate_payloads] == ["Bx", "Y"]


def test_plan_breadcrumb_navigation_descends_when_current_is_prefix() -> None:
    plan = plan_breadcrumb_navigation(
        ["device", "F"], ["device", "F", "path", "to"], [0, 1]
    )
    assert plan == NavigationPlan(action="click_down", start_index=2)


def test_plan_breadcrumb_navigation_hops_up_for_sibling() -> None:
    plan = plan_breadcrumb_navigation(
        ["device", "F", "other"], ["device", "F", "path"], [0, 1, 2]
    )
    assert plan == NavigationPlan(
        action="breadcrumb_up", start_index=2, hop_address_index=1
    )


def test_plan_breadcrumb_navigation_hops_up_for_cousin() -> None:
    plan = plan_breadcrumb_navigation(
        ["device", "A", "deep", "leaf"], ["device", "B", "target"], [0, 1, 2, 3]
    )
    assert plan == NavigationPlan(
        action="breadcrumb_up", start_index=1, hop_address_index=0
    )


def test_plan_breadcrumb_navigation_hops_up_to_reach_ancestor() -> None:
    plan = plan_breadcrumb_navigation(
        ["device", "F", "sub"], ["device", "F"], [0, 1, 2]
    )
    assert plan == NavigationPlan(
        action="breadcrumb_up", start_index=2, hop_address_index=1
    )


def test_plan_breadcrumb_navigation_noop_when_already_at_target() -> None:
    plan = plan_breadcrumb_navigation(["device", "F"], ["device", "F"], [0, 1])
    assert plan == NavigationPlan(action="click_down", start_index=2)


def test_plan_breadcrumb_navigation_hops_to_deepest_visible_when_collapsed() -> None:
    # Deep path whose breadcrumb has collapsed the middle crumbs: only the device
    # (0) and the current leaf (6) remain clickable. The plan must still hop up to
    # the surviving shared ancestor (device) instead of restarting from home.
    current = ["device", "DRIVE", "F1", "F2", "F3", "F4", "F5"]
    target = ["device", "DRIVE", "F1", "F2", "F3", "F4", "F6"]
    plan = plan_breadcrumb_navigation(current, target, [0, 6])
    assert plan == NavigationPlan(
        action="breadcrumb_up", start_index=1, hop_address_index=0
    )


def test_plan_breadcrumb_navigation_climbs_when_shared_ancestor_is_collapsed() -> None:
    # Diverges at index 2 (A vs Ax) but only the two deepest crumbs are clickable;
    # the shared ancestors are hidden behind the ellipsis. Climb to the shallowest
    # visible crumb (B, addressindex 3) rather than restarting from home.
    current = ["device", "DRIVE", "A", "B", "C"]
    target = ["device", "DRIVE", "Ax", "Z"]
    plan = plan_breadcrumb_navigation(current, target, [3, 4])
    assert plan == NavigationPlan(
        action="breadcrumb_climb", start_index=0, hop_address_index=3
    )


def test_plan_breadcrumb_navigation_goes_home_when_only_leaf_crumb_visible() -> None:
    # Nothing above the current folder is clickable, so there is nowhere to climb.
    plan = plan_breadcrumb_navigation(
        ["device", "F", "sub"], ["device", "F", "other"], [2]
    )
    assert plan == NavigationPlan(action="go_home", start_index=0)


def test_plan_breadcrumb_navigation_goes_home_for_different_root() -> None:
    plan = plan_breadcrumb_navigation(["otherdevice", "X"], ["device", "F"], [0, 1])
    assert plan == NavigationPlan(action="go_home", start_index=0)


def test_plan_breadcrumb_navigation_goes_home_when_current_is_empty() -> None:
    plan = plan_breadcrumb_navigation([], ["device", "F"], [])
    assert plan == NavigationPlan(action="go_home", start_index=0)


def test_download_one_file_uses_bounded_download_start_timeout(
    tmp_path: Path,
) -> None:
    page = FakeDownloadTimeoutPage()

    with pytest.raises(RuntimeError, match="stale or blocked download"):
        download_one_file(
            cast(Page, page),
            remote_file=_remote_file("example.mp3"),
            staging_dir=tmp_path,
            cooldown_ms=1500,
        )

    assert page.expect_download_timeout == DOWNLOAD_START_TIMEOUT_MS


def test_download_one_file_closes_leftover_error_tab_on_timeout(
    tmp_path: Path,
) -> None:
    # A rejected path (INVALID PATH) leaves the download tab open showing the JSON
    # error; the timeout path must close it while leaving unrelated tabs alone.
    error_tab = FakeTab(
        "https://evsweb5505.idrive.com/evs/v1/downloadFile?version=0&p=%2F%2FDRIVE"
        "%2FTools%2FRoslyn%2FSystem.Security.Cryptography.Cng.dll"
    )
    unrelated_tab = FakeTab("https://www.idrive.com/idrive/home/device/F")
    page = FakeDownloadTimeoutPage(
        leftover_tab=error_tab, existing_tabs=[unrelated_tab]
    )

    with pytest.raises(RuntimeError, match="stale or blocked download"):
        download_one_file(
            cast(Page, page),
            remote_file=_remote_file("System.Security.Cryptography.Cng.dll"),
            staging_dir=tmp_path,
            cooldown_ms=1500,
        )

    assert error_tab.closed is True
    assert unrelated_tab.closed is False


def test_download_one_file_reaps_error_tab_left_from_a_previous_download(
    tmp_path: Path,
) -> None:
    # An INVALID PATH error tab stranded by an EARLIER download is already present
    # before this one starts (so it is not a "new" tab). It must still be reaped, or
    # such tabs accumulate across a long run and eventually destabilize the browser.
    stale_error_tab = FakeTab(
        "https://evsweb5505.idrive.com/evs/v1/downloadFile?version=0&p=%2Ffoo.dll"
    )
    page = FakeDownloadTimeoutPage(existing_tabs=[stale_error_tab])

    with pytest.raises(RuntimeError, match="stale or blocked download"):
        download_one_file(
            cast(Page, page),
            remote_file=_remote_file("bar.dll"),
            staging_dir=tmp_path,
            cooldown_ms=1500,
        )

    assert stale_error_tab.closed is True


def test_download_one_file_fails_fast_when_trigger_reports_missing_row(
    tmp_path: Path,
) -> None:
    page = FakeDownloadTriggerFailurePage()

    with pytest.raises(RuntimeError, match="File row not found"):
        download_one_file(
            cast(Page, page),
            remote_file=_remote_file("example.mp3"),
            staging_dir=tmp_path,
            cooldown_ms=1500,
        )

    assert page.evaluate_payload == {
        "fileName": "example.mp3",
        "rowIndex": 1,
        "cooldownMs": 1500,
    }


def test_download_one_file_returns_artifact_path_without_copying(
    tmp_path: Path,
) -> None:
    # Owned local browser: the artifact already sits on the staging volume, so
    # download_one_file hands back its path and never streams a copy.
    artifact_path = tmp_path / "staging" / "guid-artifact"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(b"payload")
    download = FakeDownload(artifact_path=artifact_path)
    page = FakeDownloadPage(download)

    staged_path = download_one_file(
        cast(Page, page),
        _remote_file("example.mp3"),
        tmp_path / "staging",
        cooldown_ms=1500,
    )

    assert staged_path == artifact_path
    assert download.save_as_target is None


def test_download_one_file_streams_copy_to_staging_when_path_unavailable(
    tmp_path: Path,
) -> None:
    # CDP-attached browser: download.path() is unavailable, so the file is
    # streamed onto the staging volume under its suggested name.
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    download = FakeDownload(artifact_path=None, suggested_filename="example.mp3")
    page = FakeDownloadPage(download)

    staged_path = download_one_file(
        cast(Page, page),
        _remote_file("example.mp3"),
        staging_dir,
        cooldown_ms=1500,
    )

    assert staged_path == staging_dir / "example.mp3"
    assert download.save_as_target == str(staging_dir / "example.mp3")


def test_download_one_file_raises_when_download_reports_failure(
    tmp_path: Path,
) -> None:
    download = FakeDownload(artifact_path=None, failure="user canceled")
    page = FakeDownloadPage(download)

    with pytest.raises(RuntimeError, match="Download failed"):
        download_one_file(
            cast(Page, page),
            _remote_file("example.mp3"),
            tmp_path,
            cooldown_ms=1500,
        )


def _fake_load_folder_entries_cache(
    downloads_dir: Path,
    target_url: str,
) -> None:
    return None


def _fake_evaluate_current_folder_entries(page: object) -> RemoteEntries:
    return RemoteEntries(files=[], folders=[])


def _fake_write_folder_entries_cache(
    downloads_dir: Path,
    target_url: str,
    entries: RemoteEntries,
) -> None:
    return None


class FakeLoadPage:
    def __init__(self, url: str) -> None:
        self.url = url
        self.navigated_urls: list[str] = []

    def goto(self, url: str, *, wait_until: str) -> None:
        assert wait_until == "domcontentloaded"
        self.url = url
        self.navigated_urls.append(url)

    def wait_for_timeout(self, timeout: float) -> None:
        pass


class FakeClosedPage:
    def __init__(self) -> None:
        self.url = "https://www.idrive.com/idrive/home/device/F/folder"

    def is_closed(self) -> bool:
        return True


class FakeOpenPage:
    def is_closed(self) -> bool:
        return False


class FakeClosesDuringSleepPage:
    # Reports open on the first is_closed() call (the post-failure abort check),
    # then closed on every later call (during the sliced backoff and after).
    def __init__(self) -> None:
        self.url = "https://www.idrive.com/idrive/home/device/F/folder"
        self._checks = 0

    def is_closed(self) -> bool:
        self._checks += 1
        return self._checks > 1


def _fake_ensure_authenticated_page(
    page: Page,
    *,
    target_url: str,
    allow_interactive_login: bool,
) -> None:
    return None


def _fake_wait_for_folder_view_settle(page: object, timeout_ms: int) -> None:
    return None


def _fake_load_js_asset(name: str) -> str:
    if name == "click_breadcrumb_by_index.js":
        return "fake breadcrumb script"
    assert name == "click_folder_by_name.js"
    return "fake folder script"


class FakeClickPage(FakeLoadPage):
    def __init__(
        self, url: str, breadcrumb_address_indexes: list[int] | None = None
    ) -> None:
        super().__init__(url)
        self.evaluate_payloads: list[FolderClickPayload] = []
        self.breadcrumb_clicks: list[dict[str, object]] = []
        self._breadcrumb_address_indexes = breadcrumb_address_indexes or []

    def evaluate(self, expression: str, payload: object = None) -> object:
        if payload is None:
            # Inline breadcrumb-read expression (single argument).
            return list(self._breadcrumb_address_indexes)
        if expression == "fake breadcrumb script":
            self.breadcrumb_clicks.append(cast(dict[str, object], payload))
            return {"ok": True}
        assert expression == "fake folder script"
        self.evaluate_payloads.append(cast(FolderClickPayload, payload))
        return {"ok": True}


class FakeUnavailableClickPage(FakeLoadPage):
    """A page whose folder click always reports IDrive's "problem" error box."""

    def __init__(self, url: str) -> None:
        super().__init__(url)
        self.folder_click_calls = 0

    def evaluate(self, expression: str, payload: object = None) -> object:
        if payload is None:
            # Inline breadcrumb-read expression: no crumbs visible.
            return []
        assert expression == "fake folder script"
        self.folder_click_calls += 1
        return {
            "ok": False,
            "folderUnavailable": True,
            "reason": (
                'IDrive refused to open folder "device": '
                "There is some problem. Try later."
            ),
        }


class FakeUnavailableLoadPage:
    """An open page whose URL never matches the target, used to exercise the
    quick-retry-then-skip path when navigation keeps raising unavailability."""

    def __init__(self, url: str) -> None:
        self.url = url

    def is_closed(self) -> bool:
        return False

    def wait_for_timeout(self, timeout: float) -> None:
        pass


class FakeClimbPage(FakeLoadPage):
    """A page where clicking a breadcrumb crumb navigates up and re-renders the
    breadcrumb, revealing crumbs that were collapsed at the deeper location."""

    def __init__(
        self,
        url: str,
        breadcrumb_address_indexes: list[int],
        on_breadcrumb_click: dict[int, tuple[str, list[int]]],
    ) -> None:
        super().__init__(url)
        self._visible = list(breadcrumb_address_indexes)
        self._on_click = on_breadcrumb_click
        self.breadcrumb_clicks: list[int] = []
        self.evaluate_payloads: list[FolderClickPayload] = []

    def evaluate(self, expression: str, payload: object = None) -> object:
        if payload is None:
            return list(self._visible)
        if expression == "fake breadcrumb script":
            address_index = cast(int, cast(dict[str, object], payload)["addressIndex"])
            self.breadcrumb_clicks.append(address_index)
            transition = self._on_click.get(address_index)
            if transition is not None:
                self.url, new_visible = transition
                self._visible = list(new_visible)
            return {"ok": True}
        assert expression == "fake folder script"
        self.evaluate_payloads.append(cast(FolderClickPayload, payload))
        return {"ok": True}


class FakeTab:
    def __init__(self, url: str) -> None:
        self._url = url
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed

    @property
    def url(self) -> str:
        return self._url

    def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self, pages: list[object]) -> None:
        self.pages = pages


class FakeDownloadTimeoutPage:
    def __init__(
        self,
        *,
        leftover_tab: FakeTab | None = None,
        existing_tabs: list[FakeTab] | None = None,
    ) -> None:
        self.expect_download_timeout: float | None = None
        self.context = FakeContext([self, *(existing_tabs or [])])
        self._leftover_tab = leftover_tab

    def expect_download(self, *, timeout: float) -> "FakeDownloadTimeoutWaiter":
        self.expect_download_timeout = timeout
        # Simulate IDrive opening the download tab during the trigger; on a
        # rejected path the tab lingers rather than turning into an attachment.
        if self._leftover_tab is not None:
            self.context.pages.append(self._leftover_tab)
        return FakeDownloadTimeoutWaiter()


class FakeDownloadTimeoutWaiter:
    def __enter__(self) -> object:
        raise PlaywrightTimeoutError("download did not start")

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        return None


class FakeDownloadTriggerFailurePage:
    def __init__(self) -> None:
        self.evaluate_payload: object | None = None
        self.context = FakeContext([self])

    def expect_download(self, *, timeout: float) -> "FakeDownloadTriggerFailureWaiter":
        assert timeout == DOWNLOAD_START_TIMEOUT_MS
        return FakeDownloadTriggerFailureWaiter()

    def evaluate(self, expression: str, payload: object) -> dict[str, object]:
        self.evaluate_payload = payload
        return {"ok": False, "reason": "File row not found after scrolling"}


class FakeDownloadTriggerFailureWaiter:
    def __enter__(self) -> "FakeDownloadTriggerFailureWaiter":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        return None

    @property
    def value(self) -> object:
        raise AssertionError("download value should not be read after trigger failure")


class FakeDownload:
    def __init__(
        self,
        *,
        artifact_path: Path | None,
        suggested_filename: str = "example.mp3",
        failure: str | None = None,
    ) -> None:
        self._artifact_path = artifact_path
        self.suggested_filename = suggested_filename
        self._failure = failure
        self.save_as_target: str | None = None

    def failure(self) -> str | None:
        return self._failure

    def path(self) -> Path:
        if self._artifact_path is None:
            raise PlaywrightError(
                "Path is not available when using browser_type.connect()."
            )
        return self._artifact_path

    def save_as(self, target: str) -> None:
        self.save_as_target = target


class FakeDownloadWaiter:
    def __init__(self, download: FakeDownload) -> None:
        self._download = download

    def __enter__(self) -> "FakeDownloadWaiter":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        return None

    @property
    def value(self) -> FakeDownload:
        return self._download


class FakeDownloadPage:
    def __init__(self, download: FakeDownload) -> None:
        self._download = download
        self.evaluate_payload: object | None = None
        self.context = FakeContext([self])

    def expect_download(self, *, timeout: float) -> FakeDownloadWaiter:
        assert timeout == DOWNLOAD_START_TIMEOUT_MS
        return FakeDownloadWaiter(self._download)

    def evaluate(self, expression: str, payload: object) -> dict[str, object]:
        self.evaluate_payload = payload
        return {"ok": True}


def _remote_file(file_name: str) -> RemoteFile:
    return RemoteFile(
        file_name=file_name,
        row_index=1,
        server_size_text=None,
        server_modified_text=None,
    )


class FakeHungPage:
    """Still open — nothing closed it — but folder loads never finish."""

    def __init__(self) -> None:
        self.url = "https://www.idrive.com/idrive/home/device/parent"

    def is_closed(self) -> bool:
        return False


def _patch_hang(monkeypatch: pytest.MonkeyPatch, settle_error: Exception) -> None:
    def raise_settle_error(*_args: object, **_kwargs: object) -> None:
        raise settle_error

    def do_nothing(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.navigate_to_folder_with_clicks",
        do_nothing,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.ensure_authenticated_page",
        do_nothing,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.wait_for_folder_view_settle",
        raise_settle_error,
    )
    # The real backoff would sleep 10s between attempts; the hang must be reported
    # from the attempt count, not from waiting anyone out.
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page._sleep_before_retry",
        do_nothing,
    )


@pytest.mark.parametrize(
    "settle_error",
    [
        FolderLoadTimeoutError("Timed out waiting for folder loader to finish"),
        PlaywrightTimeoutError("page.evaluate: Timeout 120000ms exceeded"),
    ],
)
def test_load_folder_reports_a_hang_instead_of_grinding_the_retry_window(
    monkeypatch: pytest.MonkeyPatch,
    settle_error: Exception,
) -> None:
    # A wedged page fails every load the same way. Retrying it inside the two-hour
    # folder-load window would stall the run for hours on one folder, so the loads
    # are counted and a hang is reported to the caller, which recycles the browser.
    _patch_hang(monkeypatch, settle_error)

    with pytest.raises(BrowserHungError, match="stopped responding"):
        ensure_folder_loaded_for_download(
            cast(Page, FakeHungPage()),
            target_url="https://www.idrive.com/idrive/home/device/parent/child",
            timeout_ms=120_000,
            allow_interactive_login=False,
            expected_folder_name="child",
        )


def test_load_folder_entries_propagates_a_hang_without_retrying_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    load_calls = 0

    def hung_load_folder_with_retry(*_args: object, **_kwargs: object) -> None:
        nonlocal load_calls
        load_calls += 1
        raise BrowserHungError("the page or the browser has stopped responding")

    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page.load_folder_entries_cache",
        _fake_load_folder_entries_cache,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_page._load_folder_with_retry",
        hung_load_folder_with_retry,
    )

    with pytest.raises(BrowserHungError):
        load_folder_entries_with_retry(
            cast(Page, FakeHungPage()),
            downloads_dir=tmp_path,
            target_url="https://example.com/folder",
            timeout_ms=60_000,
            allow_interactive_login=True,
            expected_folder_name=None,
            use_folder_cache=True,
        )

    # The outer window must not re-run a load that already reported a hang: the
    # browser has to be recycled first, and only the caller can do that.
    assert load_calls == 1


def test_folder_load_timeout_limit_is_small_enough_to_notice_a_hang_quickly() -> None:
    # Two 120s loads (~4 minutes) instead of the two-hour retry window.
    assert FOLDER_LOAD_TIMEOUT_LIMIT == 2
