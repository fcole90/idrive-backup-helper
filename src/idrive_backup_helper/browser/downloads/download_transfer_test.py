from pathlib import Path
from typing import Any

import pytest

from idrive_backup_helper.browser.downloads.download_models import RemoteFile
from idrive_backup_helper.browser.downloads.download_transfer import (
    transfer_remote_file_to_destination,
)


def test_transfer_remote_file_to_destination_builds_downloaded_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    staging_dir = tmp_path / "staging"
    destination_dir = tmp_path / "dest"
    staging_dir.mkdir(parents=True)
    destination_dir.mkdir(parents=True)

    staged_path = staging_dir / "example.txt"
    final_path = destination_dir / "example.txt"

    def fake_download_one_file(
        page: object,
        remote_file: RemoteFile,
        staging_dir_arg: Path,
        cooldown_ms: int,
    ) -> Path:
        assert staging_dir_arg == staging_dir
        assert cooldown_ms == 1500
        assert remote_file.file_name == "example.txt"
        return staged_path

    def fake_move_download_to_destination(
        source_path: Path,
        destination: Path,
        file_name: str,
        *,
        replace_existing: bool,
    ) -> Path:
        assert source_path == staged_path
        assert destination == destination_dir
        assert file_name == "example.txt"
        assert replace_existing is True
        return final_path

    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_transfer.download_one_file",
        fake_download_one_file,
    )
    monkeypatch.setattr(
        "idrive_backup_helper.browser.downloads.download_transfer.move_download_to_destination",
        fake_move_download_to_destination,
    )

    page_stub: Any = object()
    downloaded = transfer_remote_file_to_destination(
        page=page_stub,
        remote_file=RemoteFile(
            file_name="example.txt",
            row_index=1,
            server_size_text=None,
            server_modified_text=None,
        ),
        staging_dir=staging_dir,
        destination_dir=destination_dir,
        replace_existing=True,
        cooldown_ms=1500,
    )

    assert downloaded.file_name == "example.txt"
    assert downloaded.staged_path == staged_path
    assert downloaded.final_path == final_path
