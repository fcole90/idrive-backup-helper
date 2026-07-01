from collections.abc import Iterator
from datetime import datetime
import json
import os
from pathlib import Path
from types import TracebackType
from typing import cast

from idrive_backup_helper.browser.downloads.download_models import (
    DownloadCounts,
    DownloadedFile,
    FailedFile,
    ManifestFileRecord,
    ManifestHeader,
    ManifestVerification,
    SkippedFile,
)

# ndjson manifest: line 0 is a `run` header, then one record per discovered/
# downloaded/skipped/failed file, then a trailing `summary` line. This keeps both
# the write and read paths O(1) in memory instead of holding one object per file
# for the whole run. Legacy pretty-printed JSON manifests (single object with
# `discoveredFiles`/`downloaded`/... arrays) are still read for verify/retry/resume.
MANIFEST_VERSION = "download-folder-ndjson-1"


def build_manifest_path(downloads_dir: Path, started_at: datetime) -> Path:
    timestamp = started_at.strftime("%Y-%m-%dT%H-%M-%S")
    return downloads_dir / f"download-folder-run-{timestamp}.ndjson"


def build_retry_manifest_path(downloads_dir: Path, started_at: datetime) -> Path:
    timestamp = started_at.strftime("%Y-%m-%dT%H-%M-%S")
    return downloads_dir / f"retry-manifest-run-{timestamp}.ndjson"


def ensure_destination_dir(destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    return destination


def _serialize_path(path: Path, repo_root: Path) -> str:
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path)


def relative_path_from_destination(base_destination: Path, final_path: Path) -> str:
    try:
        return str(final_path.relative_to(base_destination))
    except ValueError:
        return final_path.name


class StreamingManifestWriter:
    """Append per-file manifest records to disk as they are produced.

    Only counts are held in memory, so the footprint stays ~O(1) in file count for
    the whole run instead of growing one object per discovered/downloaded/skipped/
    failed file. Records stream to a sibling ``<manifest>.partial`` journal, flushed
    per record so a process freeze/OOM leaves a usable partial file. ``finalize``
    appends the summary line and atomically ``os.replace``-s the journal into the
    real manifest path, so a reader or glob never sees a half-written run.
    """

    def __init__(
        self,
        *,
        manifest_path: Path,
        repo_root: Path,
        url: str,
        destination: Path,
        started_at: datetime,
        progress_log_path: Path | None,
    ) -> None:
        self.manifest_path = manifest_path
        self._partial_path = manifest_path.with_name(manifest_path.name + ".partial")
        self._repo_root = repo_root
        self._destination = destination
        self._discovered = 0
        self._downloaded = 0
        self._skipped = 0
        self._failed = 0
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._partial_path.open("w", encoding="utf-8")
        self._write_line(
            {
                "type": "run",
                "version": MANIFEST_VERSION,
                "url": url,
                "destination": str(destination),
                "startedAt": started_at.isoformat(timespec="seconds"),
                "progressLogPath": (
                    str(progress_log_path) if progress_log_path is not None else None
                ),
            }
        )

    def __enter__(self) -> "StreamingManifestWriter":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        # On success `finalize` has already closed the handle and renamed the
        # partial into place. On an error path this just closes the handle and
        # leaves the partial journal on disk so the streamed records survive.
        if not self._handle.closed:
            self._handle.close()

    def _write_line(self, record: dict[str, object]) -> None:
        # Flush (not fsync) per record: cheap, and enough to survive a Python-level
        # crash/OOM because the bytes reach the OS page cache. This replaces the old
        # in-memory list append that grew O(files) across the whole run.
        self._handle.write(json.dumps(record) + "\n")
        self._handle.flush()

    def record_discovered(self, record: ManifestFileRecord) -> None:
        self._write_line(
            {
                "type": "discovered",
                "folderUrl": record.folder_url,
                "relativePath": record.relative_path,
                "fileName": record.file_name,
                "finalPath": str(record.final_path),
                "serverSizeText": record.server_size_text,
                "serverModifiedText": record.server_modified_text,
            }
        )
        self._discovered += 1

    def record_downloaded(self, item: DownloadedFile) -> None:
        self._write_line(
            {
                "type": "downloaded",
                "fileName": item.file_name,
                "relativePath": relative_path_from_destination(
                    self._destination, item.final_path
                ),
                "stagedPath": _serialize_path(item.staged_path, self._repo_root),
                "finalPath": str(item.final_path),
            }
        )
        self._downloaded += 1

    def record_skipped(self, item: SkippedFile) -> None:
        self._write_line(
            {
                "type": "skipped",
                "fileName": item.file_name,
                "reason": item.reason,
                "finalPath": (
                    str(item.final_path) if item.final_path is not None else None
                ),
            }
        )
        self._skipped += 1

    def record_failed(self, item: FailedFile) -> None:
        self._write_line(
            {
                "type": "failed",
                "fileName": item.file_name,
                "reason": item.reason,
                "finalPath": (
                    str(item.final_path) if item.final_path is not None else None
                ),
            }
        )
        self._failed += 1

    @property
    def counts(self) -> DownloadCounts:
        return DownloadCounts(
            discovered=self._discovered,
            downloaded=self._downloaded,
            skipped=self._skipped,
            failed=self._failed,
        )

    def finalize(self, finished_at: datetime) -> DownloadCounts:
        counts = self.counts
        self._write_line(
            {
                "type": "summary",
                "finishedAt": finished_at.isoformat(timespec="seconds"),
                "discovered": counts.discovered,
                "downloaded": counts.downloaded,
                "skipped": counts.skipped,
                "failed": counts.failed,
            }
        )
        self._handle.close()
        # Atomic publish onto the same volume as the journal: readers and globs only
        # ever see a complete manifest, never a partially written run.
        os.replace(self._partial_path, self.manifest_path)
        return counts


def _is_ndjson_manifest(manifest_path: Path) -> bool:
    return manifest_path.suffix == ".ndjson"


def _parse_ndjson_line(
    line: str,
    manifest_path: Path,
    index: int,
) -> dict[object, object]:
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"Invalid manifest line {index} in {manifest_path}: {error}"
        ) from error
    if not isinstance(parsed, dict):
        raise RuntimeError(
            f"Manifest line {index} in {manifest_path} is not an object."
        )
    return cast(dict[object, object], parsed)


def _iter_ndjson_records(
    manifest_path: Path,
) -> Iterator[tuple[int, dict[object, object]]]:
    with manifest_path.open("r", encoding="utf-8") as handle:
        for index, line in enumerate(handle):
            if not line.strip():
                continue
            yield index, _parse_ndjson_line(line, manifest_path, index)


def _load_legacy_json_object(manifest_path: Path) -> dict[object, object]:
    raw_object = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw_object, dict):
        raise RuntimeError("Manifest must contain a JSON object.")
    return cast(dict[object, object], raw_object)


def _require_str(value: object, field: str, index: int) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"Invalid {field} at index {index}.")
    return value


def _optional_str(value: object, field: str, index: int) -> str | None:
    if value is not None and not isinstance(value, str):
        raise RuntimeError(f"Invalid {field} at index {index}.")
    return value


def _parse_discovered_record(
    record: dict[object, object],
    index: int,
) -> ManifestFileRecord:
    return ManifestFileRecord(
        folder_url=_require_str(record.get("folderUrl"), "folderUrl", index),
        relative_path=_require_str(record.get("relativePath"), "relativePath", index),
        file_name=_require_str(record.get("fileName"), "fileName", index),
        final_path=Path(_require_str(record.get("finalPath"), "finalPath", index)),
        server_size_text=_optional_str(
            record.get("serverSizeText"), "serverSizeText", index
        ),
        server_modified_text=_optional_str(
            record.get("serverModifiedText"), "serverModifiedText", index
        ),
    )


def read_manifest_header(manifest_path: Path) -> ManifestHeader:
    """Read run-level metadata (url, destination) without materializing records."""
    if _is_ndjson_manifest(manifest_path):
        with manifest_path.open("r", encoding="utf-8") as handle:
            first_line = handle.readline()
        raw = _parse_ndjson_line(first_line, manifest_path, 0)
        if raw.get("type") != "run":
            raise RuntimeError("Manifest does not start with a run header line.")
    else:
        raw = _load_legacy_json_object(manifest_path)

    url = raw.get("url")
    destination = raw.get("destination")
    if not isinstance(url, str) or not url:
        raise RuntimeError("Manifest is missing a valid 'url'.")
    if not isinstance(destination, str) or not destination:
        raise RuntimeError("Manifest is missing a valid 'destination'.")
    return ManifestHeader(
        manifest_path=manifest_path,
        url=url,
        destination=Path(destination),
    )


def iter_manifest_discovered_files(
    manifest_path: Path,
) -> Iterator[ManifestFileRecord]:
    """Stream the discovered-file inventory one record at a time (both formats)."""
    if _is_ndjson_manifest(manifest_path):
        for index, record in _iter_ndjson_records(manifest_path):
            if record.get("type") != "discovered":
                continue
            yield _parse_discovered_record(record, index)
        return

    raw = _load_legacy_json_object(manifest_path)
    discovered_files = raw.get("discoveredFiles")
    if not isinstance(discovered_files, list):
        raise RuntimeError(
            "Manifest lacks discoveredFiles inventory. Re-run download-folder "
            "with the current version first."
        )
    for index, item_object in enumerate(cast(list[object], discovered_files)):
        if not isinstance(item_object, dict):
            raise RuntimeError(f"Invalid discoveredFiles item at index {index}.")
        yield _parse_discovered_record(cast(dict[object, object], item_object), index)


def verify_download_manifest(manifest_path: Path) -> ManifestVerification:
    expected_files = 0
    missing_files = 0
    for record in iter_manifest_discovered_files(manifest_path):
        expected_files += 1
        if not record.final_path.exists():
            missing_files += 1

    return ManifestVerification(
        manifest_path=manifest_path,
        expected_files=expected_files,
        present_files=expected_files - missing_files,
        missing_files=missing_files,
    )
