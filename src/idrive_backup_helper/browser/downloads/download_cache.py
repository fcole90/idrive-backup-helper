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
from idrive_backup_helper.browser.downloads.folder_urls import normalized_folder_url

# Bump ONLY when the serialized `entries` shape or field semantics change in a way
# that makes older payloads unusable by `parse_remote_entries`. Do NOT bump for
# browser-script or unrelated code changes: a nested-folder crawl can take hours,
# and the cached listing stays valid across those. Browser scripts are always
# reloaded from disk at crawl time, so reusing the cache never serves a stale
# script.
FOLDER_ENTRIES_DATA_VERSION = 2

# Cache payloads written with one of these versions are structurally compatible
# with the current parser. They are adopted (and re-stamped to the current
# version) instead of forcing a re-crawl. `None` covers legacy caches written
# before the version field existed.
_ADOPTABLE_DATA_VERSIONS: frozenset[object] = frozenset({None, 2})


def _folder_cache_dir(downloads_dir: Path) -> Path:
    return downloads_dir / "folder-cache"


def _folder_cache_path(downloads_dir: Path, folder_url: str) -> Path:
    # Key the cache on the normalized URL so trivial differences (trailing slash,
    # casing, query-parameter order) resolve to the same entry instead of forcing
    # a fresh crawl.
    canonical_url = normalized_folder_url(folder_url)
    url_hash = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()
    return _folder_cache_dir(downloads_dir) / f"{url_hash}.json"


def _legacy_folder_cache_path(downloads_dir: Path, folder_url: str) -> Path:
    # Older caches were keyed on the raw, un-normalized URL. Kept so existing
    # entries can be adopted and migrated to the normalized key on first read.
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
        "cacheVersion": FOLDER_ENTRIES_DATA_VERSION,
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
    legacy_path = _legacy_folder_cache_path(downloads_dir, folder_url)

    if cache_path.exists():
        source_path = cache_path
    elif legacy_path.exists():
        source_path = legacy_path
    else:
        return None

    try:
        payload_object = json.loads(source_path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return None

    if not isinstance(payload_object, dict):
        return None

    payload = cast(dict[object, object], payload_object)
    cache_version = payload.get("cacheVersion")
    if cache_version not in _ADOPTABLE_DATA_VERSIONS:
        return None

    entries_object = payload.get("entries")
    if not isinstance(entries_object, list):
        return None

    try:
        entries = parse_remote_entries(ensure_raw_file_list(entries_object))
    except ValueError:
        return None

    # Lazily migrate a compatible cache so the expensive crawl data is preserved
    # and not re-validated again: re-stamp an older payload version and/or re-key a
    # legacy (raw-URL) entry under the current normalized-URL filename.
    stale_version = cache_version != FOLDER_ENTRIES_DATA_VERSION
    legacy_key = source_path != cache_path
    if stale_version or legacy_key:
        write_folder_entries_cache(downloads_dir, folder_url, entries)
    if legacy_key:
        legacy_path.unlink(missing_ok=True)

    return entries


def _manifest_paths(downloads_dir: Path) -> list[Path]:
    # Both the legacy pretty-printed `.json` manifests and the current streamed
    # `.ndjson` manifests count for resume. `<name>.ndjson.partial` journals from a
    # crashed run end in `.partial`, so `*.ndjson` never matches them.
    return sorted(
        [
            *downloads_dir.glob("download-folder-run-*.json"),
            *downloads_dir.glob("download-folder-run-*.ndjson"),
            *downloads_dir.glob("retry-manifest-run-*.json"),
            *downloads_dir.glob("retry-manifest-run-*.ndjson"),
        ]
    )


def _manifest_sort_key(manifest_path: Path) -> tuple[str, float]:
    mtime = manifest_path.stat().st_mtime
    try:
        if manifest_path.suffix == ".ndjson":
            # ndjson records `finishedAt` only in its trailing summary line, but the
            # header's `startedAt` (line 0) is cheap to read and orders sequential
            # runs identically for the "latest state wins" merge below.
            with manifest_path.open("r", encoding="utf-8") as handle:
                first_line = handle.readline()
            if first_line.strip():
                header_object = json.loads(first_line)
                if isinstance(header_object, dict):
                    started_at = cast(dict[object, object], header_object).get(
                        "startedAt"
                    )
                    if isinstance(started_at, str):
                        return (started_at, mtime)
            return ("", mtime)

        payload_object = json.loads(manifest_path.read_text(encoding="utf-8"))
        if isinstance(payload_object, dict):
            payload = cast(dict[object, object], payload_object)
            finished_at = payload.get("finishedAt")
            if isinstance(finished_at, str):
                return (finished_at, mtime)
    except OSError, json.JSONDecodeError:
        pass

    return ("", mtime)


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


def _apply_legacy_json_manifest(
    manifest_path: Path,
    *,
    url: str,
    destination: Path,
    status_by_path: dict[str, bool],
) -> None:
    try:
        payload_object = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return

    if not isinstance(payload_object, dict):
        return

    payload = cast(dict[object, object], payload_object)
    # Match on the folder URL only. Success is keyed by the URL and the
    # destination-relative path, so resume knowledge stays valid when the
    # `--to` destination changes (or the tree is copied to another machine).
    # The skip decision in download_run re-checks that the file is actually
    # present at the current destination before trusting this index.
    if payload.get("url") != url:
        return

    for section, succeeded in (
        ("downloaded", True),
        ("skipped", True),
        ("failed", False),
    ):
        section_object = payload.get(section)
        if not isinstance(section_object, list):
            continue
        for item in cast(list[object], section_object):
            relative_path = _extract_relative_path(item, destination)
            if relative_path is not None:
                status_by_path[relative_path] = succeeded


def _apply_ndjson_manifest(
    manifest_path: Path,
    *,
    url: str,
    destination: Path,
    status_by_path: dict[str, bool],
) -> None:
    # Same URL-keyed, destination-relative success index as the legacy path, read
    # one record per line so a huge manifest never lands fully in memory.
    success_by_type = {"downloaded": True, "skipped": True, "failed": False}
    matched_url = False
    try:
        with manifest_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    record_object = json.loads(line)
                except json.JSONDecodeError:
                    return
                if not isinstance(record_object, dict):
                    continue
                record = cast(dict[object, object], record_object)
                record_type = record.get("type")
                if record_type == "run":
                    if record.get("url") != url:
                        return
                    matched_url = True
                    continue
                # Ignore outcome records until the run header has confirmed the URL,
                # so a headerless/foreign file never leaks into this URL's index.
                if not matched_url:
                    continue
                if (
                    not isinstance(record_type, str)
                    or record_type not in success_by_type
                ):
                    continue
                relative_path = _extract_relative_path(record, destination)
                if relative_path is not None:
                    status_by_path[relative_path] = success_by_type[record_type]
    except OSError:
        return


def load_resume_success_relative_paths(
    downloads_dir: Path,
    *,
    url: str,
    destination: Path,
) -> set[str]:
    status_by_path: dict[str, bool] = {}

    for manifest_path in sorted(_manifest_paths(downloads_dir), key=_manifest_sort_key):
        if manifest_path.suffix == ".ndjson":
            _apply_ndjson_manifest(
                manifest_path,
                url=url,
                destination=destination,
                status_by_path=status_by_path,
            )
        else:
            _apply_legacy_json_manifest(
                manifest_path,
                url=url,
                destination=destination,
                status_by_path=status_by_path,
            )

    return {path for path, succeeded in status_by_path.items() if succeeded}
