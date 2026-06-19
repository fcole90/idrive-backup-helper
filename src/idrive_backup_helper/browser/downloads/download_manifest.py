from datetime import datetime
import json
from pathlib import Path
from typing import cast

from idrive_backup_helper.browser.downloads.download_models import (
    DownloadFolderReport,
    DownloadManifest,
    ManifestFileRecord,
    ManifestVerification,
)


def build_manifest_path(downloads_dir: Path, started_at: datetime) -> Path:
    timestamp = started_at.strftime("%Y-%m-%dT%H-%M-%S")
    return downloads_dir / f"download-folder-run-{timestamp}.json"


def build_retry_manifest_path(downloads_dir: Path, started_at: datetime) -> Path:
    timestamp = started_at.strftime("%Y-%m-%dT%H-%M-%S")
    return downloads_dir / f"retry-manifest-run-{timestamp}.json"


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


def write_manifest(report: DownloadFolderReport, repo_root: Path) -> None:
    manifest = {
        "version": "poc-download-folder-1.0",
        "url": report.url,
        "destination": str(report.destination),
        "startedAt": report.started_at.isoformat(timespec="seconds"),
        "finishedAt": report.finished_at.isoformat(timespec="seconds"),
        "progressLogPath": (
            str(report.progress_log_path)
            if report.progress_log_path is not None
            else None
        ),
        "downloaded": [
            {
                "fileName": item.file_name,
                "relativePath": relative_path_from_destination(
                    report.destination,
                    item.final_path,
                ),
                "stagedPath": _serialize_path(item.staged_path, repo_root),
                "finalPath": str(item.final_path),
            }
            for item in report.downloaded
        ],
        "skipped": [
            {
                "fileName": item.file_name,
                "reason": item.reason,
                "finalPath": (
                    str(item.final_path) if item.final_path is not None else None
                ),
            }
            for item in report.skipped
        ],
        "failed": [
            {
                "fileName": item.file_name,
                "reason": item.reason,
                "finalPath": (
                    str(item.final_path) if item.final_path is not None else None
                ),
            }
            for item in report.failed
        ],
        "discoveredFiles": [
            {
                "folderUrl": item.folder_url,
                "relativePath": item.relative_path,
                "fileName": item.file_name,
                "finalPath": str(item.final_path),
                "serverSizeText": item.server_size_text,
                "serverModifiedText": item.server_modified_text,
            }
            for item in report.discovered_files
        ],
    }
    report.manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )


def load_download_manifest(manifest_path: Path) -> DownloadManifest:
    raw_object = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw_object, dict):
        raise RuntimeError("Manifest must contain a JSON object.")

    raw_data = cast(dict[object, object], raw_object)

    url = raw_data.get("url")
    destination = raw_data.get("destination")
    discovered_files = raw_data.get("discoveredFiles")

    if not isinstance(url, str) or not url:
        raise RuntimeError("Manifest is missing a valid 'url'.")
    if not isinstance(destination, str) or not destination:
        raise RuntimeError("Manifest is missing a valid 'destination'.")
    if not isinstance(discovered_files, list):
        raise RuntimeError(
            "Manifest lacks discoveredFiles inventory. Re-run download-folder with the current version first."
        )

    discovered_file_objects = cast(list[object], discovered_files)

    parsed_discovered_files: list[ManifestFileRecord] = []
    for index, item_object in enumerate(discovered_file_objects):
        if not isinstance(item_object, dict):
            raise RuntimeError(f"Invalid discoveredFiles item at index {index}.")

        item = cast(dict[object, object], item_object)

        folder_url = item.get("folderUrl")
        relative_path = item.get("relativePath")
        file_name = item.get("fileName")
        final_path = item.get("finalPath")
        server_size_text = item.get("serverSizeText")
        server_modified_text = item.get("serverModifiedText")

        if not isinstance(folder_url, str) or not folder_url:
            raise RuntimeError(f"Invalid folderUrl at index {index}.")
        if not isinstance(relative_path, str) or not relative_path:
            raise RuntimeError(f"Invalid relativePath at index {index}.")
        if not isinstance(file_name, str) or not file_name:
            raise RuntimeError(f"Invalid fileName at index {index}.")
        if not isinstance(final_path, str) or not final_path:
            raise RuntimeError(f"Invalid finalPath at index {index}.")
        if server_size_text is not None and not isinstance(server_size_text, str):
            raise RuntimeError(f"Invalid serverSizeText at index {index}.")
        if server_modified_text is not None and not isinstance(
            server_modified_text, str
        ):
            raise RuntimeError(f"Invalid serverModifiedText at index {index}.")

        parsed_discovered_files.append(
            ManifestFileRecord(
                folder_url=folder_url,
                relative_path=relative_path,
                file_name=file_name,
                final_path=Path(final_path),
                server_size_text=server_size_text,
                server_modified_text=server_modified_text,
            )
        )

    return DownloadManifest(
        manifest_path=manifest_path,
        url=url,
        destination=Path(destination),
        discovered_files=parsed_discovered_files,
    )


def verify_download_manifest(manifest_path: Path) -> ManifestVerification:
    manifest = load_download_manifest(manifest_path)
    missing_files = [
        item for item in manifest.discovered_files if not item.final_path.exists()
    ]
    present_files = len(manifest.discovered_files) - len(missing_files)

    return ManifestVerification(
        manifest_path=manifest_path,
        expected_files=len(manifest.discovered_files),
        present_files=present_files,
        missing_files=missing_files,
    )
