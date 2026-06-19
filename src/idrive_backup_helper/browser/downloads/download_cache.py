from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import cast

from idrive_backup_helper.browser.downloads.download_entries import (
    ensure_raw_file_list,
    parse_remote_entries,
)
from idrive_backup_helper.browser.downloads.download_models import RemoteEntries

FOLDER_ENTRIES_CACHE_VERSION = 2


def _folder_cache_dir(downloads_dir: Path) -> Path:
    return downloads_dir / "folder-cache"


def _folder_cache_path(downloads_dir: Path, folder_url: str) -> Path:
    url_hash = hashlib.sha256(folder_url.encode("utf-8")).hexdigest()
    return _folder_cache_dir(downloads_dir) / f"{url_hash}.json"


def _serialize_remote_entries(entries: RemoteEntries) -> list[dict[str, object]]:
    serialized: list[dict[str, object]] = []
    for remote_folder in entries.folders:
        serialized.append(
            {
                "entryType": "folder",
                "folderName": remote_folder.folder_name,
                "href": remote_folder.href,
            }
        )
    for remote_file in entries.files:
        serialized.append(
            {
                "entryType": "file",
                "fileName": remote_file.file_name,
                "rowIndex": remote_file.row_index,
                "serverSizeText": remote_file.server_size_text,
                "serverModifiedText": remote_file.server_modified_text,
            }
        )
    return serialized


def write_folder_entries_cache(
    downloads_dir: Path,
    folder_url: str,
    entries: RemoteEntries,
) -> None:
    cache_dir = _folder_cache_dir(downloads_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = _folder_cache_path(downloads_dir, folder_url)
    payload = {
        "cacheVersion": FOLDER_ENTRIES_CACHE_VERSION,
        "url": folder_url,
        "cachedAt": datetime.now().isoformat(timespec="seconds"),
        "entries": _serialize_remote_entries(entries),
    }
    cache_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def load_folder_entries_cache(
    downloads_dir: Path,
    folder_url: str,
) -> RemoteEntries | None:
    cache_path = _folder_cache_path(downloads_dir, folder_url)
    if not cache_path.exists():
        return None

    try:
        payload_object = json.loads(cache_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None

    if not isinstance(payload_object, dict):
        return None

    payload = cast(dict[object, object], payload_object)
    if payload.get("cacheVersion") != FOLDER_ENTRIES_CACHE_VERSION:
        return None

    entries_object = payload.get("entries")
    if not isinstance(entries_object, list):
        return None

    try:
        return parse_remote_entries(ensure_raw_file_list(entries_object))
    except ValueError:
        return None


def _manifest_paths(downloads_dir: Path) -> list[Path]:
    return sorted(
        [
            *downloads_dir.glob("download-folder-run-*.json"),
            *downloads_dir.glob("retry-manifest-run-*.json"),
        ]
    )


def _manifest_sort_key(manifest_path: Path) -> tuple[str, float]:
    try:
        payload_object = json.loads(manifest_path.read_text(encoding="utf-8"))
        if isinstance(payload_object, dict):
            payload = cast(dict[object, object], payload_object)
            finished_at = payload.get("finishedAt")
            if isinstance(finished_at, str):
                return (finished_at, manifest_path.stat().st_mtime)
    except OSError, json.JSONDecodeError:
        pass

    return ("", manifest_path.stat().st_mtime)


def _extract_relative_path(
    item_object: object,
    destination: Path,
) -> str | None:
    if not isinstance(item_object, dict):
        return None

    item = cast(dict[object, object], item_object)
    relative_path = item.get("relativePath")
    if isinstance(relative_path, str) and relative_path:
        return relative_path

    final_path = item.get("finalPath")
    if isinstance(final_path, str) and final_path:
        final_path_object = Path(final_path)
        try:
            return str(final_path_object.relative_to(destination))
        except ValueError:
            return None

    return None


def load_resume_success_relative_paths(
    downloads_dir: Path,
    *,
    url: str,
    destination: Path,
) -> set[str]:
    status_by_path: dict[str, bool] = {}

    for manifest_path in sorted(_manifest_paths(downloads_dir), key=_manifest_sort_key):
        try:
            payload_object = json.loads(manifest_path.read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
            continue

        if not isinstance(payload_object, dict):
            continue

        payload = cast(dict[object, object], payload_object)
        manifest_url = payload.get("url")
        manifest_destination = payload.get("destination")
        if manifest_url != url or manifest_destination != str(destination):
            continue

        downloaded_object = payload.get("downloaded")
        if isinstance(downloaded_object, list):
            downloaded_items = cast(list[object], downloaded_object)
            for item in downloaded_items:
                relative_path = _extract_relative_path(item, destination)
                if relative_path is not None:
                    status_by_path[relative_path] = True

        skipped_object = payload.get("skipped")
        if isinstance(skipped_object, list):
            skipped_items = cast(list[object], skipped_object)
            for item in skipped_items:
                relative_path = _extract_relative_path(item, destination)
                if relative_path is not None:
                    status_by_path[relative_path] = True

        failed_object = payload.get("failed")
        if isinstance(failed_object, list):
            failed_items = cast(list[object], failed_object)
            for item in failed_items:
                relative_path = _extract_relative_path(item, destination)
                if relative_path is not None:
                    status_by_path[relative_path] = False

    return {path for path, succeeded in status_by_path.items() if succeeded}
