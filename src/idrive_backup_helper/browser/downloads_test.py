import pytest

from idrive_backup_helper.browser.downloads import (
    DownloadFolderReport,
    FailedFile,
    build_manifest_path,
    ensure_destination_dir,
    ensure_raw_file_list,
    parse_remote_files,
)
from datetime import datetime
from pathlib import Path


def test_parse_remote_files_accepts_valid_payload() -> None:
    payload = [
        {
            "fileName": "example.pdf",
            "rowIndex": 3,
            "serverSizeText": "12.4 MB",
            "serverModifiedText": "06/10/2026 14:02",
        }
    ]

    parsed = parse_remote_files(ensure_raw_file_list(payload))

    assert len(parsed) == 1
    assert parsed[0].file_name == "example.pdf"
    assert parsed[0].row_index == 3


def test_parse_remote_files_rejects_missing_file_name() -> None:
    payload = [{"rowIndex": 0}]

    with pytest.raises(ValueError, match="fileName"):
        parse_remote_files(ensure_raw_file_list(payload))


def test_parse_remote_files_rejects_non_list_payload() -> None:
    with pytest.raises(ValueError, match="JSON array"):
        ensure_raw_file_list({"fileName": "bad"})


def test_build_manifest_path_uses_expected_file_name() -> None:
    manifest_path = build_manifest_path(
        Path("/tmp/downloads"),
        datetime(2026, 6, 14, 14, 30, 0),
    )

    assert manifest_path.name == "download-folder-run-2026-06-14T14-30-00.json"


def test_ensure_destination_dir_creates_missing_directory(tmp_path: Path) -> None:
    destination = tmp_path / "nested" / "output"

    result = ensure_destination_dir(destination)

    assert result == destination
    assert destination.exists()
    assert destination.is_dir()


def test_download_folder_report_exit_code_tracks_failures() -> None:
    clean_report = DownloadFolderReport(
        url="https://example.com",
        destination=Path("/tmp/out"),
        started_at=datetime(2026, 6, 14, 14, 30, 0),
        finished_at=datetime(2026, 6, 14, 14, 31, 0),
        downloaded=[],
        skipped=[],
        failed=[],
        manifest_path=Path("/tmp/downloads/report.json"),
    )
    failed_report = DownloadFolderReport(
        url="https://example.com",
        destination=Path("/tmp/out"),
        started_at=datetime(2026, 6, 14, 14, 30, 0),
        finished_at=datetime(2026, 6, 14, 14, 31, 0),
        downloaded=[],
        skipped=[],
        failed=[FailedFile(file_name="bad.zip", reason="Download timed out")],
        manifest_path=Path("/tmp/downloads/report.json"),
    )

    assert clean_report.exit_code == 0
    assert failed_report.exit_code == 1
