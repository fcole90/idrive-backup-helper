import hashlib
import json
import pytest

from idrive_backup_helper.browser.downloads.downloads import (
    RemoteEntries,
    RemoteFile,
    DownloadFolderReport,
    FailedFile,
    ProgressEventLogger,
    RemoteFolder,
    build_progress_log_path,
    load_folder_entries_cache,
    load_resume_success_relative_paths,
    write_folder_entries_cache,
    build_manifest_path,
    build_retry_manifest_path,
    ensure_destination_dir,
    ensure_raw_file_list,
    load_download_manifest,
    parse_remote_entries,
    parse_remote_files,
    verify_download_manifest,
)
from idrive_backup_helper.browser.downloads.folder_urls import normalized_folder_url
from datetime import datetime
from pathlib import Path


def test_parse_remote_files_accepts_valid_payload() -> None:
    payload = [
        {
            "entryType": "file",
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
    payload = [{"entryType": "file", "rowIndex": 0}]

    with pytest.raises(ValueError, match="fileName"):
        parse_remote_files(ensure_raw_file_list(payload))


def test_parse_remote_files_rejects_non_list_payload() -> None:
    with pytest.raises(ValueError, match="JSON array"):
        ensure_raw_file_list({"fileName": "bad"})


def test_parse_remote_entries_collects_folders_and_files() -> None:
    payload = [
        {
            "entryType": "folder",
            "folderName": "photos",
            "href": "https://example.com/photos",
        },
        {
            "entryType": "file",
            "fileName": "example.pdf",
            "rowIndex": 3,
            "serverSizeText": "12.4 MB",
            "serverModifiedText": "06/10/2026 14:02",
        },
    ]

    entries = parse_remote_entries(ensure_raw_file_list(payload))

    assert len(entries.folders) == 1
    assert entries.folders[0] == RemoteFolder(
        folder_name="photos",
        href="https://example.com/photos",
    )
    assert len(entries.files) == 1


def test_build_manifest_path_uses_expected_file_name() -> None:
    manifest_path = build_manifest_path(
        Path("/tmp/downloads"),
        datetime(2026, 6, 14, 14, 30, 0),
    )

    assert manifest_path.name == "download-folder-run-2026-06-14T14-30-00.json"


def test_build_retry_manifest_path_uses_expected_file_name() -> None:
    manifest_path = build_retry_manifest_path(
        Path("/tmp/downloads"),
        datetime(2026, 6, 14, 14, 30, 0),
    )

    assert manifest_path.name == "retry-manifest-run-2026-06-14T14-30-00.json"


def test_build_progress_log_path_uses_expected_file_name() -> None:
    log_path = build_progress_log_path(
        Path("/tmp/downloads"),
        datetime(2026, 6, 14, 14, 30, 0),
        prefix="download-folder-progress",
    )

    assert log_path.name == "download-folder-progress-2026-06-14T14-30-00.ndjson"


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
        discovered_files=[],
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
        discovered_files=[],
        manifest_path=Path("/tmp/downloads/report.json"),
    )

    assert clean_report.exit_code == 0
    assert failed_report.exit_code == 1


def test_load_download_manifest_rejects_legacy_manifest(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        '{"url":"https://example.com","destination":"/tmp/out"}\n',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="discoveredFiles"):
        load_download_manifest(manifest_path)


def test_verify_download_manifest_reports_missing_files(tmp_path: Path) -> None:
    final_path = tmp_path / "out" / "example.txt"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "url": "https://example.com",
                "destination": str(tmp_path / "out"),
                "discoveredFiles": [
                    {
                        "folderUrl": "https://example.com/folder",
                        "relativePath": "example.txt",
                        "fileName": "example.txt",
                        "finalPath": str(final_path),
                        "serverSizeText": "1 KB",
                        "serverModifiedText": "2026-06-14",
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    verification = verify_download_manifest(manifest_path)

    assert verification.expected_files == 1
    assert verification.present_files == 0
    assert len(verification.missing_files) == 1


def test_progress_event_logger_appends_jsonl_records(tmp_path: Path) -> None:
    log_path = tmp_path / "events.ndjson"
    logger = ProgressEventLogger(log_path)

    logger.log("run_started", mode="download-folder")
    logger.log("file_skipped", fileName="example.txt", reason="destination exists")

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2

    first_event = json.loads(lines[0])
    second_event = json.loads(lines[1])

    assert first_event["event"] == "run_started"
    assert first_event["sequence"] == 0
    assert first_event["mode"] == "download-folder"
    assert second_event["event"] == "file_skipped"
    assert second_event["sequence"] == 1
    assert second_event["fileName"] == "example.txt"


def test_folder_entries_cache_roundtrip(tmp_path: Path) -> None:
    downloads_dir = tmp_path / "downloads"
    entries = RemoteEntries(
        files=[
            RemoteFile(
                file_name="example.txt",
                row_index=1,
                server_size_text="1 KB",
                server_modified_text="2026-06-15",
            )
        ],
        folders=[
            RemoteFolder(
                folder_name="nested",
                href="https://example.com/nested",
            )
        ],
    )

    write_folder_entries_cache(downloads_dir, "https://example.com/root", entries)
    cached = load_folder_entries_cache(downloads_dir, "https://example.com/root")

    assert cached == entries


def test_folder_entries_cache_adopts_legacy_unversioned_payload(
    tmp_path: Path,
) -> None:
    downloads_dir = tmp_path / "downloads"
    cache_dir = downloads_dir / "folder-cache"
    cache_dir.mkdir(parents=True)
    folder_url = "https://example.com/root"
    cache_hash = hashlib.sha256(folder_url.encode("utf-8")).hexdigest()
    cache_path = cache_dir / f"{cache_hash}.json"
    cache_path.write_text(
        json.dumps(
            {
                "url": folder_url,
                "cachedAt": "2026-06-17T12:00:00",
                "entries": [
                    {
                        "entryType": "folder",
                        "folderName": "kept",
                        "href": "https://example.com/kept",
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    cached = load_folder_entries_cache(downloads_dir, folder_url)

    # The expensive crawl data is reused, not discarded, across the version bump.
    assert cached == RemoteEntries(
        files=[],
        folders=[RemoteFolder(folder_name="kept", href="https://example.com/kept")],
    )
    # And the legacy payload is re-stamped to the current version in place, so it
    # is not re-validated on the next read.
    migrated_payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert migrated_payload["cacheVersion"] == 2


def test_folder_entries_cache_rejects_unparseable_payload(tmp_path: Path) -> None:
    downloads_dir = tmp_path / "downloads"
    cache_dir = downloads_dir / "folder-cache"
    cache_dir.mkdir(parents=True)
    folder_url = "https://example.com/root"
    cache_hash = hashlib.sha256(folder_url.encode("utf-8")).hexdigest()
    cache_path = cache_dir / f"{cache_hash}.json"
    cache_path.write_text(
        json.dumps(
            {
                "cacheVersion": 2,
                "url": folder_url,
                "entries": [{"entryType": "folder", "folderName": "missing-href"}],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    # A structurally broken payload still forces a re-crawl of that one folder.
    assert load_folder_entries_cache(downloads_dir, folder_url) is None


def test_folder_entries_cache_matches_equivalent_url_variants(tmp_path: Path) -> None:
    downloads_dir = tmp_path / "downloads"
    entries = RemoteEntries(
        files=[],
        folders=[RemoteFolder(folder_name="nested", href="https://example.com/nested")],
    )

    write_folder_entries_cache(downloads_dir, "https://example.com/root", entries)

    # A trailing-slash variant of the same folder URL resolves to the same cache
    # entry instead of missing and triggering a fresh crawl.
    assert (
        load_folder_entries_cache(downloads_dir, "https://example.com/root/") == entries
    )


def test_folder_entries_cache_migrates_legacy_raw_url_key(tmp_path: Path) -> None:
    downloads_dir = tmp_path / "downloads"
    cache_dir = downloads_dir / "folder-cache"
    cache_dir.mkdir(parents=True)

    # A URL whose normalized form differs from the raw string (trailing slash), so
    # the legacy raw-URL hash and the normalized hash are distinct files.
    request_url = "https://example.com/root/"
    raw_hash = hashlib.sha256(request_url.encode("utf-8")).hexdigest()
    normalized_hash = hashlib.sha256(
        normalized_folder_url(request_url).encode("utf-8")
    ).hexdigest()
    assert raw_hash != normalized_hash

    legacy_path = cache_dir / f"{raw_hash}.json"
    legacy_path.write_text(
        json.dumps(
            {
                "cacheVersion": 2,
                "url": request_url,
                "entries": [
                    {
                        "entryType": "folder",
                        "folderName": "kept",
                        "href": "https://example.com/kept",
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    cached = load_folder_entries_cache(downloads_dir, request_url)

    assert cached == RemoteEntries(
        files=[],
        folders=[RemoteFolder(folder_name="kept", href="https://example.com/kept")],
    )
    # The entry is re-keyed under the normalized-URL filename and the legacy
    # raw-URL file is removed.
    assert (cache_dir / f"{normalized_hash}.json").exists()
    assert not legacy_path.exists()


def test_resume_success_paths_keep_latest_state(tmp_path: Path) -> None:
    downloads_dir = tmp_path / "downloads"
    downloads_dir.mkdir(parents=True)
    destination = tmp_path / "out"
    destination.mkdir(parents=True)

    first_manifest = {
        "url": "https://example.com/root",
        "destination": str(destination),
        "finishedAt": "2026-06-15T10:00:00",
        "downloaded": [
            {
                "fileName": "a.txt",
                "relativePath": "a.txt",
                "finalPath": str(destination / "a.txt"),
            }
        ],
        "skipped": [],
        "failed": [
            {
                "fileName": "b.txt",
                "finalPath": str(destination / "b.txt"),
            }
        ],
    }
    second_manifest = {
        "url": "https://example.com/root",
        "destination": str(destination),
        "finishedAt": "2026-06-15T11:00:00",
        "downloaded": [
            {
                "fileName": "b.txt",
                "relativePath": "b.txt",
                "finalPath": str(destination / "b.txt"),
            }
        ],
        "skipped": [],
        "failed": [
            {
                "fileName": "a.txt",
                "finalPath": str(destination / "a.txt"),
            }
        ],
    }

    (downloads_dir / "download-folder-run-2026-06-15T10-00-00.json").write_text(
        json.dumps(first_manifest) + "\n",
        encoding="utf-8",
    )
    (downloads_dir / "retry-manifest-run-2026-06-15T11-00-00.json").write_text(
        json.dumps(second_manifest) + "\n",
        encoding="utf-8",
    )

    successful_paths = load_resume_success_relative_paths(
        downloads_dir,
        url="https://example.com/root",
        destination=destination,
    )

    assert successful_paths == {"b.txt"}


def test_resume_success_paths_are_destination_independent(tmp_path: Path) -> None:
    downloads_dir = tmp_path / "downloads"
    downloads_dir.mkdir(parents=True)
    old_destination = tmp_path / "old-out"
    new_destination = tmp_path / "new-out"

    manifest = {
        "url": "https://example.com/root",
        "destination": str(old_destination),
        "finishedAt": "2026-06-15T10:00:00",
        "downloaded": [
            {
                "fileName": "a.txt",
                "relativePath": "nested/a.txt",
                "finalPath": str(old_destination / "nested" / "a.txt"),
            }
        ],
        "skipped": [],
        "failed": [],
    }
    (downloads_dir / "download-folder-run-2026-06-15T10-00-00.json").write_text(
        json.dumps(manifest) + "\n",
        encoding="utf-8",
    )

    # Querying with a different destination than the manifest recorded still
    # surfaces the prior success, because resume knowledge is keyed by URL and the
    # destination-relative path rather than the absolute destination.
    successful_paths = load_resume_success_relative_paths(
        downloads_dir,
        url="https://example.com/root",
        destination=new_destination,
    )

    assert successful_paths == {"nested/a.txt"}
