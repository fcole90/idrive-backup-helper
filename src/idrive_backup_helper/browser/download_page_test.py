from pathlib import Path
from typing import cast

import pytest
from playwright.sync_api import Page

from idrive_backup_helper.browser.download_models import RemoteEntries
from idrive_backup_helper.browser.download_page import (
    FOLDER_SETTLE_STABLE_TICKS,
    SelectorState,
    load_folder_entries_with_retry,
    wait_for_folder_view_settle,
)


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


def test_wait_for_folder_view_settle_waits_for_loader_to_disappear(
    capsys: pytest.CaptureFixture[str],
) -> None:
    page = FakeFolderPage(
        [
            {"loaderVisible": True, "contentRowCount": 0, "totalRowCount": 1},
            {"loaderVisible": True, "contentRowCount": 2, "totalRowCount": 3},
            *[
                {"loaderVisible": False, "contentRowCount": 2, "totalRowCount": 2}
                for _ in range(FOLDER_SETTLE_STABLE_TICKS + 1)
            ],
        ]
    )

    wait_for_folder_view_settle(page, timeout_ms=60_000)

    output = capsys.readouterr().out
    assert "Folder loader still visible" in output
    assert "Folder view settled (2 content row(s), 2 total row(s))" in output
    assert len(page.waited_timeouts) >= FOLDER_SETTLE_STABLE_TICKS


def test_load_folder_entries_accepts_settled_empty_folder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    load_calls = 0

    def fake_load_folder_with_retry(*args: object, **kwargs: object) -> None:
        nonlocal load_calls
        load_calls += 1

    monkeypatch.setattr(
        "idrive_backup_helper.browser.download_page.load_folder_entries_cache",
        _fake_load_folder_entries_cache,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.download_page._load_folder_with_retry",
        fake_load_folder_with_retry,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.download_page._evaluate_current_folder_entries",
        _fake_evaluate_current_folder_entries,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.download_page.write_folder_entries_cache",
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
