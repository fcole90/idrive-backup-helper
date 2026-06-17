from pathlib import Path

import pytest

from idrive_backup_helper.browser import download_run
from idrive_backup_helper.browser.download_models import (
    DownloadedFile,
    RemoteEntries,
    RemoteFile,
)
from idrive_backup_helper.browser.download_run import download_current_folder


class FakeBrowserEngine:
    def __init__(self, config: object) -> None:
        self.config = config
        self.page = object()

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
        downloads_dir: Path,
        destination_dir: Path,
        replace_existing: bool,
        cooldown_ms: int,
    ) -> DownloadedFile:
        assert load_calls == [folder_url]
        staged_path = downloads_dir / remote_file.file_name
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
    assert [skipped.file_name for skipped in report.skipped] == ["already.txt"]
    assert [downloaded.file_name for downloaded in report.downloaded] == ["needed.txt"]


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

    assert any(message == "Discovered remote file: already.txt" for message in messages)
    assert any(
        message.startswith(
            "Skipping file without checking IDrive row because destination already exists"
        )
        for message in messages
    )
    assert any(
        message == "Attempting IDrive download for remote file: needed.txt"
        for message in messages
    )


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
    downloads_dir: Path,
    destination_dir: Path,
    replace_existing: bool,
    cooldown_ms: int,
) -> DownloadedFile:
    staged_path = downloads_dir / remote_file.file_name
    final_path = destination_dir / remote_file.file_name
    return DownloadedFile(
        file_name=remote_file.file_name,
        staged_path=staged_path,
        final_path=final_path,
    )
