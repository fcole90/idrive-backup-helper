import json
from pathlib import Path
from typing import cast

import pytest

from idrive_backup_helper.browser.downloads import download_run
from idrive_backup_helper.browser.downloads.download_models import (
    DownloadedFile,
    RemoteEntries,
    RemoteFile,
    RemoteFolder,
)
from idrive_backup_helper.browser.downloads.download_page import BrowserClosedError
from idrive_backup_helper.browser.downloads.download_run import (
    TAB_DEATH_RECOVERY_LIMIT,
    download_current_folder,
)
from idrive_backup_helper.browser.engine import BrowserHealthReport


class FakeBrowserEngine:
    def __init__(self, config: object) -> None:
        self.config = config
        self.page = object()
        self.new_page_calls = 0

    def __enter__(self) -> "FakeBrowserEngine":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        return None

    def current_page_or_new_page(self) -> object:
        return self.page

    def new_page(self) -> object:
        # The browser is "alive": reopening a page after a tab death succeeds.
        self.new_page_calls += 1
        return object()

    def describe_browser_health(self) -> BrowserHealthReport:
        return BrowserHealthReport(
            mode="attached-cdp",
            cdp_url="http://127.0.0.1:9222",
            cdp_reachable=False,
            cdp_version=None,
            detached_pid=None,
            detached_exit_code=None,
            detached_running=None,
            chromium_log_tail=None,
        )


def test_download_current_folder_loads_cached_folder_before_first_download(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    downloads_dir = repo_root / ".agents" / "playground" / "downloads"
    profile_dir = repo_root / ".agents" / "playground" / "browser-state"
    destination = tmp_path / "destination"
    downloads_dir.mkdir(parents=True)
    profile_dir.mkdir(parents=True)
    destination.mkdir()
    (destination / "already.txt").write_text("done", encoding="utf-8")
    folder_url = "https://example.com/folder"
    load_calls: list[str] = []

    monkeypatch.setattr(download_run, "BrowserEngine", FakeBrowserEngine)
    monkeypatch.setattr(
        download_run,
        "load_folder_entries_with_retry",
        _fake_load_folder_entries_with_retry,
    )

    def fake_ensure_folder_loaded_for_download(
        page: object,
        *,
        target_url: str,
        timeout_ms: int,
        allow_interactive_login: bool,
        expected_folder_name: str | None,
    ) -> None:
        assert target_url == folder_url
        assert timeout_ms == 60_000
        assert allow_interactive_login is True
        assert expected_folder_name is None
        load_calls.append(target_url)

    def fake_transfer_remote_file_to_destination(
        *,
        page: object,
        remote_file: RemoteFile,
        staging_dir: Path,
        destination_dir: Path,
        replace_existing: bool,
        cooldown_ms: int,
    ) -> DownloadedFile:
        assert load_calls == [folder_url]
        staging_dir.mkdir(parents=True, exist_ok=True)
        staged_path = staging_dir / remote_file.file_name
        final_path = destination_dir / remote_file.file_name
        staged_path.write_text("downloaded", encoding="utf-8")
        return DownloadedFile(
            file_name=remote_file.file_name,
            staged_path=staged_path,
            final_path=final_path,
        )

    monkeypatch.setattr(
        download_run,
        "ensure_folder_loaded_for_download",
        fake_ensure_folder_loaded_for_download,
    )
    monkeypatch.setattr(
        download_run,
        "transfer_remote_file_to_destination",
        fake_transfer_remote_file_to_destination,
    )

    report = download_current_folder(
        profile_dir=profile_dir,
        downloads_dir=downloads_dir,
        url=folder_url,
        destination=destination,
        headless=False,
        timeout_ms=60_000,
        cooldown_ms=1500,
        overwrite="skip",
        use_folder_cache=True,
        resume_from_logs=False,
    )

    assert load_calls == [folder_url]
    records = _read_manifest_records(report.manifest_path)
    assert _file_names(records, "skipped") == ["already.txt"]
    assert _file_names(records, "downloaded") == ["needed.txt"]
    assert report.counts.skipped == 1
    assert report.counts.downloaded == 1


def test_download_current_folder_logs_file_decisions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    downloads_dir = tmp_path / "downloads"
    profile_dir = tmp_path / "browser-state"
    destination = tmp_path / "destination"
    downloads_dir.mkdir(parents=True)
    profile_dir.mkdir(parents=True)
    destination.mkdir()
    (destination / "already.txt").write_text("done", encoding="utf-8")
    folder_url = "https://example.com/folder"
    messages: list[str] = []

    monkeypatch.setattr(download_run, "BrowserEngine", FakeBrowserEngine)
    monkeypatch.setattr(
        download_run,
        "load_folder_entries_with_retry",
        _fake_load_folder_entries_with_retry,
    )
    monkeypatch.setattr(
        download_run,
        "ensure_folder_loaded_for_download",
        _fake_ensure_folder_loaded_for_download,
    )
    monkeypatch.setattr(
        download_run,
        "transfer_remote_file_to_destination",
        _fake_transfer_remote_file_to_destination,
    )
    monkeypatch.setattr(download_run, "log_download_message", messages.append)

    download_current_folder(
        profile_dir=profile_dir,
        downloads_dir=downloads_dir,
        url=folder_url,
        destination=destination,
        headless=False,
        timeout_ms=60_000,
        cooldown_ms=1500,
        overwrite="skip",
        use_folder_cache=True,
        resume_from_logs=False,
    )

    assert any(
        message.startswith("1 file(s) already present, 1 to download in")
        for message in messages
    )
    assert any(
        message == "Attempting IDrive download for remote file: needed.txt"
        for message in messages
    )


def test_download_current_folder_redownloads_when_resume_success_file_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    downloads_dir = repo_root / ".agents" / "playground" / "downloads"
    profile_dir = repo_root / ".agents" / "playground" / "browser-state"
    destination = tmp_path / "fresh-destination"
    downloads_dir.mkdir(parents=True)
    profile_dir.mkdir(parents=True)
    destination.mkdir()
    folder_url = "https://example.com/folder"

    # A manifest from a previous run (a different destination) records both files
    # as downloaded. The files are NOT present at this fresh destination.
    manifest = {
        "url": folder_url,
        "destination": str(tmp_path / "old-destination"),
        "finishedAt": "2026-06-15T10:00:00",
        "downloaded": [
            {"fileName": "already.txt", "relativePath": "already.txt"},
            {"fileName": "needed.txt", "relativePath": "needed.txt"},
        ],
        "skipped": [],
        "failed": [],
    }
    (downloads_dir / "download-folder-run-2026-06-15T10-00-00.json").write_text(
        json.dumps(manifest) + "\n",
        encoding="utf-8",
    )

    transferred: list[str] = []

    def fake_transfer_remote_file_to_destination(
        *,
        page: object,
        remote_file: RemoteFile,
        staging_dir: Path,
        destination_dir: Path,
        replace_existing: bool,
        cooldown_ms: int,
    ) -> DownloadedFile:
        transferred.append(remote_file.file_name)
        staged_path = staging_dir / remote_file.file_name
        final_path = destination_dir / remote_file.file_name
        return DownloadedFile(
            file_name=remote_file.file_name,
            staged_path=staged_path,
            final_path=final_path,
        )

    monkeypatch.setattr(download_run, "BrowserEngine", FakeBrowserEngine)
    monkeypatch.setattr(
        download_run,
        "load_folder_entries_with_retry",
        _fake_load_folder_entries_with_retry,
    )
    monkeypatch.setattr(
        download_run,
        "ensure_folder_loaded_for_download",
        _fake_ensure_folder_loaded_for_download,
    )
    monkeypatch.setattr(
        download_run,
        "transfer_remote_file_to_destination",
        fake_transfer_remote_file_to_destination,
    )

    report = download_current_folder(
        profile_dir=profile_dir,
        downloads_dir=downloads_dir,
        url=folder_url,
        destination=destination,
        headless=False,
        timeout_ms=60_000,
        cooldown_ms=1500,
        overwrite="skip",
        use_folder_cache=True,
        resume_from_logs=True,
    )

    # Both files are re-downloaded because none exist at the fresh destination,
    # even though the resume log marks them as previously successful.
    assert sorted(transferred) == ["already.txt", "needed.txt"]
    assert report.counts.skipped == 0


def test_download_current_folder_aborts_after_tab_death_recovery_budget_exhausted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    downloads_dir = tmp_path / "downloads"
    profile_dir = tmp_path / "browser-state"
    destination = tmp_path / "destination"
    downloads_dir.mkdir(parents=True)
    profile_dir.mkdir(parents=True)
    destination.mkdir()
    folder_url = "https://example.com/folder"
    attempted: list[str] = []

    def fake_transfer_raises_browser_closed(
        *,
        page: object,
        remote_file: RemoteFile,
        staging_dir: Path,
        destination_dir: Path,
        replace_existing: bool,
        cooldown_ms: int,
    ) -> DownloadedFile:
        attempted.append(remote_file.file_name)
        raise BrowserClosedError("browser was closed mid-download")

    monkeypatch.setattr(download_run, "BrowserEngine", FakeBrowserEngine)
    monkeypatch.setattr(
        download_run,
        "load_folder_entries_with_retry",
        _fake_load_folder_entries_with_retry,
    )
    monkeypatch.setattr(
        download_run,
        "ensure_folder_loaded_for_download",
        _fake_ensure_folder_loaded_for_download,
    )
    monkeypatch.setattr(
        download_run,
        "transfer_remote_file_to_destination",
        fake_transfer_raises_browser_closed,
    )

    with pytest.raises(BrowserClosedError):
        download_current_folder(
            profile_dir=profile_dir,
            downloads_dir=downloads_dir,
            url=folder_url,
            destination=destination,
            headless=False,
            timeout_ms=60_000,
            cooldown_ms=1500,
            overwrite="replace",
            use_folder_cache=True,
            resume_from_logs=False,
        )

    # Each death aborts the folder on the first file (never marching through the
    # rest), the folder is retried on a fresh page, and once the recovery budget
    # is spent the run aborts: one attempt per death.
    assert attempted == ["already.txt"] * (TAB_DEATH_RECOVERY_LIMIT + 1)

    # Every tab death writes its own crash-diagnostics report.
    crash_reports = list(downloads_dir.glob("download-folder-crash-*.md"))
    assert len(crash_reports) == TAB_DEATH_RECOVERY_LIMIT + 1
    report_text = crash_reports[0].read_text(encoding="utf-8")
    assert "whole browser process is gone" in report_text
    assert "browser was closed mid-download" in report_text

    progress_logs = list(downloads_dir.glob("download-folder-progress-*.ndjson"))
    progress_text = "\n".join(log.read_text(encoding="utf-8") for log in progress_logs)
    assert "browser_crash_diagnostics" in progress_text
    assert progress_text.count('"tab_death_recovered"') == TAB_DEATH_RECOVERY_LIMIT


def test_download_current_folder_recovers_from_one_tab_death(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    downloads_dir = tmp_path / "downloads"
    profile_dir = tmp_path / "browser-state"
    destination = tmp_path / "destination"
    downloads_dir.mkdir(parents=True)
    profile_dir.mkdir(parents=True)
    destination.mkdir()
    folder_url = "https://example.com/folder"
    load_calls = {"count": 0}

    def flaky_load_folder_entries_with_retry(
        page: object,
        *,
        downloads_dir: Path,
        target_url: str,
        timeout_ms: int,
        allow_interactive_login: bool,
        expected_folder_name: str | None,
        use_folder_cache: bool,
    ) -> RemoteEntries:
        load_calls["count"] += 1
        if load_calls["count"] == 1:
            # First attempt: the tab's renderer dies (browser still alive).
            raise BrowserClosedError("tab renderer died mid-listing")
        return _fake_load_folder_entries_with_retry(
            page,
            downloads_dir=downloads_dir,
            target_url=target_url,
            timeout_ms=timeout_ms,
            allow_interactive_login=allow_interactive_login,
            expected_folder_name=expected_folder_name,
            use_folder_cache=use_folder_cache,
        )

    monkeypatch.setattr(download_run, "BrowserEngine", FakeBrowserEngine)
    monkeypatch.setattr(
        download_run,
        "load_folder_entries_with_retry",
        flaky_load_folder_entries_with_retry,
    )
    monkeypatch.setattr(
        download_run,
        "ensure_folder_loaded_for_download",
        _fake_ensure_folder_loaded_for_download,
    )
    monkeypatch.setattr(
        download_run,
        "transfer_remote_file_to_destination",
        _fake_transfer_remote_file_to_destination,
    )

    report = download_current_folder(
        profile_dir=profile_dir,
        downloads_dir=downloads_dir,
        url=folder_url,
        destination=destination,
        headless=False,
        timeout_ms=60_000,
        cooldown_ms=1500,
        overwrite="replace",
        use_folder_cache=True,
        resume_from_logs=False,
    )

    # The folder was retried on a fresh page and the run finished normally.
    assert load_calls["count"] == 2
    assert report.counts.downloaded == 2
    assert report.counts.failed == 0

    crash_reports = list(downloads_dir.glob("download-folder-crash-*.md"))
    assert len(crash_reports) == 1

    progress_logs = list(downloads_dir.glob("download-folder-progress-*.ndjson"))
    progress_text = "\n".join(log.read_text(encoding="utf-8") for log in progress_logs)
    assert '"tab_death_recovered"' in progress_text
    assert '"run_finished"' in progress_text
    # Per-folder memory telemetry lands in the progress log.
    assert '"resource_sample"' in progress_text


class FakeRecyclablePage:
    def __init__(self) -> None:
        self.goto_calls: list[str] = []

    def goto(self, url: str, wait_until: str | None = None) -> None:
        self.goto_calls.append(url)


_recycling_engines: list["FakeRecyclingBrowserEngine"] = []


class FakeRecyclingBrowserEngine(FakeBrowserEngine):
    def __init__(self, config: object) -> None:
        super().__init__(config)
        self.page = FakeRecyclablePage()
        _recycling_engines.append(self)


def test_download_current_folder_recycles_renderer_between_folders(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    downloads_dir = tmp_path / "downloads"
    profile_dir = tmp_path / "browser-state"
    destination = tmp_path / "destination"
    downloads_dir.mkdir(parents=True)
    profile_dir.mkdir(parents=True)
    destination.mkdir()
    folder_url = "https://www.idrive.com/idrive/home/DEVICE_12345678/F/root"
    child_url = "https://www.idrive.com/idrive/home/DEVICE_12345678/F/root/child"
    load_calls = {"count": 0}

    def stateful_load_folder_entries_with_retry(
        page: object,
        *,
        downloads_dir: Path,
        target_url: str,
        timeout_ms: int,
        allow_interactive_login: bool,
        expected_folder_name: str | None,
        use_folder_cache: bool,
    ) -> RemoteEntries:
        load_calls["count"] += 1
        if load_calls["count"] == 1:
            return RemoteEntries(
                files=[],
                folders=[RemoteFolder(folder_name="child", href=child_url)],
            )
        return RemoteEntries(files=[], folders=[])

    _recycling_engines.clear()
    monkeypatch.setattr(download_run, "PAGE_RECYCLE_FOLDER_INTERVAL", 1)
    monkeypatch.setattr(download_run, "BrowserEngine", FakeRecyclingBrowserEngine)
    monkeypatch.setattr(
        download_run,
        "load_folder_entries_with_retry",
        stateful_load_folder_entries_with_retry,
    )

    download_current_folder(
        profile_dir=profile_dir,
        downloads_dir=downloads_dir,
        url=folder_url,
        destination=destination,
        headless=False,
        timeout_ms=60_000,
        cooldown_ms=1500,
        overwrite="skip",
        use_folder_cache=True,
        resume_from_logs=False,
    )

    # Before the second folder the SPA got a hard reset back to IDrive home.
    progress_logs = list(downloads_dir.glob("download-folder-progress-*.ndjson"))
    progress_text = "\n".join(log.read_text(encoding="utf-8") for log in progress_logs)
    assert '"page_recycled"' in progress_text
    assert len(_recycling_engines) == 1
    page = _recycling_engines[0].page
    assert page.goto_calls == ["https://www.idrive.com/idrive/home"]


def _read_manifest_records(manifest_path: Path) -> list[dict[str, object]]:
    lines = manifest_path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _file_names(records: list[dict[str, object]], record_type: str) -> list[str]:
    return [
        cast(str, record["fileName"])
        for record in records
        if record.get("type") == record_type
    ]


def _fake_load_folder_entries_with_retry(
    page: object,
    *,
    downloads_dir: Path,
    target_url: str,
    timeout_ms: int,
    allow_interactive_login: bool,
    expected_folder_name: str | None,
    use_folder_cache: bool,
) -> RemoteEntries:
    return RemoteEntries(
        files=[
            RemoteFile(
                file_name="already.txt",
                row_index=1,
                server_size_text=None,
                server_modified_text=None,
            ),
            RemoteFile(
                file_name="needed.txt",
                row_index=2,
                server_size_text=None,
                server_modified_text=None,
            ),
        ],
        folders=[],
    )


def _fake_ensure_folder_loaded_for_download(
    page: object,
    *,
    target_url: str,
    timeout_ms: int,
    allow_interactive_login: bool,
    expected_folder_name: str | None,
) -> None:
    return None


def _fake_transfer_remote_file_to_destination(
    *,
    page: object,
    remote_file: RemoteFile,
    staging_dir: Path,
    destination_dir: Path,
    replace_existing: bool,
    cooldown_ms: int,
) -> DownloadedFile:
    staged_path = staging_dir / remote_file.file_name
    final_path = destination_dir / remote_file.file_name
    return DownloadedFile(
        file_name=remote_file.file_name,
        staged_path=staged_path,
        final_path=final_path,
    )
