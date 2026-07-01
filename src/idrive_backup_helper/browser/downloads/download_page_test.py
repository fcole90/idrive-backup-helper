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
    NavigationPlan,
    SelectorState,
    plan_breadcrumb_navigation,
    normalize_folder_href,
    normalize_remote_entries_hrefs,
    download_one_file,
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
        breadcrumb_titles=["device", "F"],
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
        breadcrumb_titles=["device", "F", "other"],
    )

    navigate_to_folder_with_clicks(
        cast(Page, page),
        "https://www.idrive.com/idrive/home/device/F/path/to/destination",
        60_000,
    )

    # Sibling hop: up to the shared "F" via breadcrumb, then down the rest. No home.
    assert page.navigated_urls == []
    assert [click["title"] for click in page.breadcrumb_clicks] == ["F"]
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
        breadcrumb_titles=["otherdevice", "X"],
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


def test_plan_breadcrumb_navigation_descends_when_current_is_prefix() -> None:
    plan = plan_breadcrumb_navigation(["device", "F"], ["device", "F", "path", "to"])
    assert plan == NavigationPlan(action="click_down", start_index=2)


def test_plan_breadcrumb_navigation_hops_up_for_sibling() -> None:
    plan = plan_breadcrumb_navigation(["device", "F", "other"], ["device", "F", "path"])
    assert plan == NavigationPlan(action="breadcrumb_up", start_index=2)


def test_plan_breadcrumb_navigation_hops_up_for_cousin() -> None:
    plan = plan_breadcrumb_navigation(
        ["device", "A", "deep", "leaf"], ["device", "B", "target"]
    )
    assert plan == NavigationPlan(action="breadcrumb_up", start_index=1)


def test_plan_breadcrumb_navigation_hops_up_to_reach_ancestor() -> None:
    plan = plan_breadcrumb_navigation(["device", "F", "sub"], ["device", "F"])
    assert plan == NavigationPlan(action="breadcrumb_up", start_index=2)


def test_plan_breadcrumb_navigation_noop_when_already_at_target() -> None:
    plan = plan_breadcrumb_navigation(["device", "F"], ["device", "F"])
    assert plan == NavigationPlan(action="click_down", start_index=2)


def test_plan_breadcrumb_navigation_ignores_leading_home_chrome() -> None:
    plan = plan_breadcrumb_navigation(["Home", "device", "F"], ["device", "F", "path"])
    assert plan == NavigationPlan(action="click_down", start_index=2)


def test_plan_breadcrumb_navigation_goes_home_for_different_root() -> None:
    plan = plan_breadcrumb_navigation(["otherdevice", "X"], ["device", "F"])
    assert plan == NavigationPlan(action="go_home", start_index=0)


def test_plan_breadcrumb_navigation_goes_home_when_breadcrumb_empty() -> None:
    plan = plan_breadcrumb_navigation([], ["device", "F"])
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
    if name == "click_breadcrumb_by_name.js":
        return "fake breadcrumb script"
    assert name == "click_folder_by_name.js"
    return "fake folder script"


class FakeClickPage(FakeLoadPage):
    def __init__(self, url: str, breadcrumb_titles: list[str] | None = None) -> None:
        super().__init__(url)
        self.evaluate_payloads: list[FolderClickPayload] = []
        self.breadcrumb_clicks: list[dict[str, object]] = []
        self._breadcrumb_titles = breadcrumb_titles or []

    def evaluate(self, expression: str, payload: object = None) -> object:
        if payload is None:
            # Inline breadcrumb-read expression (single argument).
            return list(self._breadcrumb_titles)
        if expression == "fake breadcrumb script":
            self.breadcrumb_clicks.append(cast(dict[str, object], payload))
            return {"ok": True}
        assert expression == "fake folder script"
        self.evaluate_payloads.append(cast(FolderClickPayload, payload))
        return {"ok": True}


class FakeDownloadTimeoutPage:
    def __init__(self) -> None:
        self.expect_download_timeout: float | None = None

    def expect_download(self, *, timeout: float) -> "FakeDownloadTimeoutWaiter":
        self.expect_download_timeout = timeout
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
