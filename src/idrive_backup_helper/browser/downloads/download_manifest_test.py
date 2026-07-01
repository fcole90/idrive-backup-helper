from datetime import datetime
import json
from pathlib import Path
import tracemalloc

from idrive_backup_helper.browser.downloads.download_cache import (
    load_resume_success_relative_paths,
)
from idrive_backup_helper.browser.downloads.download_manifest import (
    StreamingManifestWriter,
    build_manifest_path,
    iter_manifest_discovered_files,
    read_manifest_header,
    verify_download_manifest,
)
from idrive_backup_helper.browser.downloads.download_models import (
    DownloadedFile,
    FailedFile,
    ManifestFileRecord,
    SkippedFile,
)


def _discovered_record(destination: Path, name: str) -> ManifestFileRecord:
    final_path = destination / name
    return ManifestFileRecord(
        folder_url="https://example.com/folder",
        relative_path=name,
        file_name=name,
        final_path=final_path,
        server_size_text="1 KB",
        server_modified_text="2026-06-15",
    )


def _new_writer(
    *,
    manifest_path: Path,
    destination: Path,
    started_at: datetime = datetime(2026, 6, 15, 10, 0, 0),
) -> StreamingManifestWriter:
    return StreamingManifestWriter(
        manifest_path=manifest_path,
        repo_root=manifest_path.parent,
        url="https://example.com/root",
        destination=destination,
        started_at=started_at,
        progress_log_path=None,
    )


def test_streaming_writer_round_trips_records_and_publishes_atomically(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "out"
    manifest_path = tmp_path / "download-folder-run-2026-06-15T10-00-00.ndjson"
    writer = _new_writer(manifest_path=manifest_path, destination=destination)

    writer.record_discovered(_discovered_record(destination, "a.txt"))
    writer.record_discovered(_discovered_record(destination, "b.txt"))
    writer.record_downloaded(
        DownloadedFile(
            file_name="a.txt",
            staged_path=tmp_path / "staging" / "a.txt",
            final_path=destination / "a.txt",
        )
    )
    writer.record_skipped(
        SkippedFile(
            file_name="b.txt",
            reason="destination exists",
            final_path=destination / "b.txt",
        )
    )
    writer.record_failed(
        FailedFile(file_name="c.txt", reason="boom", final_path=destination / "c.txt")
    )
    counts = writer.finalize(datetime(2026, 6, 15, 10, 5, 0))

    # The partial journal is renamed into place; a reader never sees a half-run.
    assert manifest_path.exists()
    assert not manifest_path.with_name(manifest_path.name + ".partial").exists()
    assert counts.discovered == 2
    assert counts.downloaded == 1
    assert counts.skipped == 1
    assert counts.failed == 1

    header = read_manifest_header(manifest_path)
    assert header.url == "https://example.com/root"
    assert header.destination == destination

    discovered = list(iter_manifest_discovered_files(manifest_path))
    assert discovered == [
        _discovered_record(destination, "a.txt"),
        _discovered_record(destination, "b.txt"),
    ]

    lines = [
        json.loads(line)
        for line in manifest_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert lines[0]["type"] == "run"
    assert lines[-1] == {
        "type": "summary",
        "finishedAt": "2026-06-15T10:05:00",
        "discovered": 2,
        "downloaded": 1,
        "skipped": 1,
        "failed": 1,
    }


def test_streaming_writer_handles_empty_run(tmp_path: Path) -> None:
    destination = tmp_path / "out"
    manifest_path = tmp_path / "download-folder-run-2026-06-15T10-00-00.ndjson"
    writer = _new_writer(manifest_path=manifest_path, destination=destination)

    counts = writer.finalize(datetime(2026, 6, 15, 10, 5, 0))

    assert counts.discovered == 0
    verification = verify_download_manifest(manifest_path)
    assert verification.expected_files == 0
    assert verification.present_files == 0
    assert verification.missing_files == 0


def test_streaming_writer_leaves_partial_journal_on_crash(tmp_path: Path) -> None:
    destination = tmp_path / "out"
    manifest_path = tmp_path / "download-folder-run-2026-06-15T10-00-00.ndjson"
    writer = _new_writer(manifest_path=manifest_path, destination=destination)

    writer.record_discovered(_discovered_record(destination, "a.txt"))
    # Simulate a freeze/OOM: close without finalizing.
    writer.close()

    partial_path = manifest_path.with_name(manifest_path.name + ".partial")
    assert not manifest_path.exists()
    assert partial_path.exists()
    records = [
        json.loads(line)
        for line in partial_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert records[0]["type"] == "run"
    assert records[1]["type"] == "discovered"
    assert records[1]["fileName"] == "a.txt"


def test_verify_counts_present_and_missing(tmp_path: Path) -> None:
    destination = tmp_path / "out"
    destination.mkdir()
    (destination / "present.txt").write_text("here", encoding="utf-8")
    manifest_path = tmp_path / "download-folder-run-2026-06-15T10-00-00.ndjson"
    writer = _new_writer(manifest_path=manifest_path, destination=destination)
    writer.record_discovered(_discovered_record(destination, "present.txt"))
    writer.record_discovered(_discovered_record(destination, "missing.txt"))
    writer.finalize(datetime(2026, 6, 15, 10, 5, 0))

    verification = verify_download_manifest(manifest_path)

    assert verification.expected_files == 2
    assert verification.present_files == 1
    assert verification.missing_files == 1


def test_resume_reads_ndjson_manifest_outcomes(tmp_path: Path) -> None:
    downloads_dir = tmp_path / "downloads"
    downloads_dir.mkdir()
    destination = tmp_path / "out"
    manifest_path = downloads_dir / "download-folder-run-2026-06-15T10-00-00.ndjson"
    writer = _new_writer(manifest_path=manifest_path, destination=destination)
    writer.record_downloaded(
        DownloadedFile(
            file_name="a.txt",
            staged_path=downloads_dir / "staging" / "a.txt",
            final_path=destination / "a.txt",
        )
    )
    writer.record_skipped(
        SkippedFile(
            file_name="b.txt",
            reason="destination exists",
            final_path=destination / "b.txt",
        )
    )
    writer.record_failed(
        FailedFile(file_name="c.txt", reason="boom", final_path=destination / "c.txt")
    )
    writer.finalize(datetime(2026, 6, 15, 10, 5, 0))

    successful_paths = load_resume_success_relative_paths(
        downloads_dir,
        url="https://example.com/root",
        destination=destination,
    )

    # Downloaded and skipped count as success; failed does not.
    assert successful_paths == {"a.txt", "b.txt"}


def test_resume_merges_legacy_json_and_later_ndjson_latest_wins(tmp_path: Path) -> None:
    # The upgrade scenario: a mid-backup downloads dir holds an old `.json`
    # manifest plus a newer `.ndjson` one. Resume must read both and let the later
    # run's outcome win, even though `.json` sorts on finishedAt and `.ndjson` on
    # its header startedAt.
    downloads_dir = tmp_path / "downloads"
    downloads_dir.mkdir()
    destination = tmp_path / "out"
    url = "https://example.com/root"

    legacy = {
        "url": url,
        "destination": str(destination),
        "finishedAt": "2026-06-15T10:30:00",
        "downloaded": [
            {"fileName": "a.txt", "relativePath": "a.txt"},
        ],
        "skipped": [],
        # b.txt failed in the earlier run...
        "failed": [
            {"fileName": "b.txt", "finalPath": str(destination / "b.txt")},
        ],
    }
    (downloads_dir / "download-folder-run-2026-06-15T10-00-00.json").write_text(
        json.dumps(legacy) + "\n", encoding="utf-8"
    )

    writer = _new_writer(
        manifest_path=downloads_dir / "download-folder-run-2026-06-16T10-00-00.ndjson",
        destination=destination,
        started_at=datetime(2026, 6, 16, 10, 0, 0),
    )
    # ...and succeeds in the later run, so b.txt must count as done now.
    writer.record_downloaded(
        DownloadedFile(
            file_name="b.txt",
            staged_path=downloads_dir / "staging" / "b.txt",
            final_path=destination / "b.txt",
        )
    )
    writer.record_skipped(
        SkippedFile(
            file_name="c.txt",
            reason="destination exists",
            final_path=destination / "c.txt",
        )
    )
    writer.finalize(datetime(2026, 6, 16, 10, 5, 0))

    successful_paths = load_resume_success_relative_paths(
        downloads_dir,
        url=url,
        destination=destination,
    )

    assert successful_paths == {"a.txt", "b.txt", "c.txt"}


def test_streaming_write_and_verify_stay_bounded_for_large_manifest(
    tmp_path: Path,
) -> None:
    # 50k discovered records must not scale in-memory: the writer keeps only counts
    # and verify streams the file one record at a time. A materializing design would
    # hold ~50k record objects (tens of MB); the streamed path stays flat.
    record_count = 50_000
    destination = tmp_path / "out"
    manifest_path = build_manifest_path(tmp_path, datetime(2026, 6, 15, 10, 0, 0))
    writer = _new_writer(manifest_path=manifest_path, destination=destination)

    tracemalloc.start()
    for index in range(record_count):
        writer.record_discovered(_discovered_record(destination, f"file-{index}.txt"))
    counts = writer.finalize(datetime(2026, 6, 15, 12, 0, 0))
    _current, write_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert counts.discovered == record_count

    tracemalloc.start()
    verification = verify_download_manifest(manifest_path)
    _current, verify_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert verification.expected_files == record_count
    # Comfortably below what holding all 50k records in memory would need.
    assert write_peak < 5_000_000
    assert verify_peak < 5_000_000
