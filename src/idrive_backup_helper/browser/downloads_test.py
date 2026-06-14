import pytest

from idrive_backup_helper.browser.downloads import (
    ensure_raw_file_list,
    parse_remote_files,
)


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
