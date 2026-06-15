from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import time
from typing import Literal, cast

from playwright.sync_api import (
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from idrive_backup_helper.browser.engine import BrowserConfig, BrowserEngine
from idrive_backup_helper.browser.session import ensure_authenticated_page
from idrive_backup_helper.filesystem.moves import move_download_to_destination


@dataclass(frozen=True)
class RemoteFile:
    file_name: str
    row_index: int
    server_size_text: str | None
    server_modified_text: str | None


@dataclass(frozen=True)
class RemoteFolder:
    folder_name: str
    href: str


@dataclass(frozen=True)
class RemoteEntries:
    files: list[RemoteFile]
    folders: list[RemoteFolder]


@dataclass(frozen=True)
class DownloadedFile:
    file_name: str
    staged_path: Path
    final_path: Path


@dataclass(frozen=True)
class SkippedFile:
    file_name: str
    reason: str
    final_path: Path | None = None


@dataclass(frozen=True)
class FailedFile:
    file_name: str
    reason: str
    final_path: Path | None = None


@dataclass(frozen=True)
class ManifestFileRecord:
    folder_url: str
    relative_path: str
    file_name: str
    final_path: Path
    server_size_text: str | None
    server_modified_text: str | None


@dataclass(frozen=True)
class DownloadManifest:
    manifest_path: Path
    url: str
    destination: Path
    discovered_files: list[ManifestFileRecord]


@dataclass(frozen=True)
class ManifestVerification:
    manifest_path: Path
    expected_files: int
    present_files: int
    missing_files: list[ManifestFileRecord]

    @property
    def exit_code(self) -> int:
        return 1 if self.missing_files else 0


@dataclass(frozen=True)
class DownloadFolderReport:
    url: str
    destination: Path
    started_at: datetime
    finished_at: datetime
    downloaded: list[DownloadedFile]
    skipped: list[SkippedFile]
    failed: list[FailedFile]
    discovered_files: list[ManifestFileRecord]
    manifest_path: Path
    progress_log_path: Path | None = None

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0


type OverwriteMode = Literal["skip", "replace", "fail"]

FOLDER_SETTLE_POLL_MS = 1_000
FOLDER_SETTLE_STABLE_TICKS = 10
FOLDER_LOAD_RETRY_INTERVAL_MS = 10_000
FOLDER_LOAD_RETRY_TIMEOUT_MS = 120_000


@dataclass(frozen=True)
class FolderTask:
    url: str
    destination: Path
    expected_folder_name: str | None


def _log(message: str) -> None:
    print(f"[download-folder] {message}", flush=True)


def _iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def build_progress_log_path(
    downloads_dir: Path,
    started_at: datetime,
    *,
    prefix: str,
) -> Path:
    timestamp = started_at.strftime("%Y-%m-%dT%H-%M-%S")
    return downloads_dir / f"{prefix}-{timestamp}.ndjson"


class ProgressEventLogger:
    def __init__(self, progress_log_path: Path) -> None:
        self.progress_log_path = progress_log_path
        self._sequence = 0
        self._enabled = True
        self.progress_log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event_type: str, **fields: object) -> None:
        if not self._enabled:
            return

        payload: dict[str, object] = {
            "timestamp": _iso_now(),
            "sequence": self._sequence,
            "event": event_type,
            **fields,
        }

        try:
            with self.progress_log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload) + "\n")
        except OSError as error:
            _log(f"Progress log disabled after write error: {error}")
            self._enabled = False
            return

        self._sequence += 1


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


def _js_asset_path(name: str) -> Path:
    return Path(__file__).resolve().parent / "js" / name


def _load_js_asset(name: str) -> str:
    asset_path = _js_asset_path(name)
    if not asset_path.exists():
        raise RuntimeError(f"Missing browser script asset: {asset_path}")

    return asset_path.read_text(encoding="utf-8")


def _ensure_trigger_result(raw_result: object, file_name: str) -> None:
    if not isinstance(raw_result, dict):
        return

    result_dict = cast(dict[object, object], raw_result)
    ok_value = result_dict.get("ok")
    if ok_value is True:
        return

    reason_value = result_dict.get("reason")
    if isinstance(reason_value, str) and reason_value:
        raise RuntimeError(reason_value)

    raise RuntimeError(f"Download trigger failed for {file_name}")


def ensure_raw_file_list(raw_files: object) -> list[object]:
    if not isinstance(raw_files, list):
        raise ValueError("Browser file list must be a JSON array.")

    return cast(list[object], raw_files)


def parse_remote_entries(raw_entries: list[object]) -> RemoteEntries:
    files: list[RemoteFile] = []
    folders: list[RemoteFolder] = []

    for index, item_object in enumerate(raw_entries):
        if not isinstance(item_object, dict):
            raise ValueError(f"Invalid file item at index {index}: expected object.")

        candidate_dict = cast(dict[object, object], item_object)
        normalized_item: dict[str, object] = {}
        for key_object, value_object in candidate_dict.items():
            if isinstance(key_object, str):
                normalized_item[key_object] = value_object

        entry_type = normalized_item.get("entryType")
        if entry_type == "file":
            file_name = normalized_item.get("fileName")
            row_index = normalized_item.get("rowIndex")
            server_size_text = normalized_item.get("serverSizeText")
            server_modified_text = normalized_item.get("serverModifiedText")

            if not isinstance(file_name, str) or not file_name.strip():
                raise ValueError(f"Invalid fileName at index {index}.")
            if not isinstance(row_index, int):
                raise ValueError(f"Invalid rowIndex at index {index}.")
            if server_size_text is not None and not isinstance(server_size_text, str):
                raise ValueError(f"Invalid serverSizeText at index {index}.")
            if server_modified_text is not None and not isinstance(
                server_modified_text, str
            ):
                raise ValueError(f"Invalid serverModifiedText at index {index}.")

            files.append(
                RemoteFile(
                    file_name=file_name,
                    row_index=row_index,
                    server_size_text=server_size_text,
                    server_modified_text=server_modified_text,
                )
            )
            continue

        if entry_type == "folder":
            folder_name = normalized_item.get("folderName")
            href = normalized_item.get("href")

            if not isinstance(folder_name, str) or not folder_name.strip():
                raise ValueError(f"Invalid folderName at index {index}.")
            if not isinstance(href, str) or not href.strip():
                raise ValueError(f"Invalid href at index {index}.")

            folders.append(RemoteFolder(folder_name=folder_name, href=href))
            continue

        raise ValueError(f"Invalid entryType at index {index}.")

    return RemoteEntries(files=files, folders=folders)


def parse_remote_files(raw_files: list[object]) -> list[RemoteFile]:
    return parse_remote_entries(raw_files).files


def _evaluate_current_folder_entries(page: Page) -> RemoteEntries:
    script = _load_js_asset("list_current_folder_files.js")
    raw_files: object = page.evaluate(
        script,
        {
            "scrollIntervalMs": 350,
            "maxIdleTicks": 3,
        },
    )
    return parse_remote_entries(ensure_raw_file_list(raw_files))


def _wait_for_folder_view_settle(page: Page, timeout_ms: int) -> None:
    page.wait_for_selector("#file_list_container", state="attached", timeout=timeout_ms)

    deadline = time.monotonic() + (timeout_ms / 1000)
    stable_ticks = 0
    last_row_count = -1

    while time.monotonic() < deadline:
        row_count_obj: object = page.evaluate(
            "() => document.querySelectorAll('#file_list_container > li').length"
        )
        row_count = int(row_count_obj) if isinstance(row_count_obj, int) else 0

        if row_count == last_row_count:
            stable_ticks += 1
        else:
            stable_ticks = 0
            last_row_count = row_count

        if stable_ticks >= FOLDER_SETTLE_STABLE_TICKS:
            return

        page.wait_for_timeout(FOLDER_SETTLE_POLL_MS)


def _read_breadcrumb_titles(page: Page) -> list[str]:
    raw_titles: object = page.evaluate("""
() => {
  const breadcrumb = document.querySelector('div.breadcrumb');
  if (!breadcrumb) {
    return [];
  }

  return [...breadcrumb.childNodes]
    .filter((node) => node.nodeType === 1)
    .map((node) => node.title || '')
    .filter((title) => title);
}
""")
    if not isinstance(raw_titles, list):
        return []

    typed_titles = cast(list[object], raw_titles)
    return [title for title in typed_titles if isinstance(title, str)]


def _ensure_expected_folder_loaded(
    page: Page, expected_folder_name: str | None
) -> None:
    if expected_folder_name is None:
        return

    breadcrumb_titles = _read_breadcrumb_titles(page)
    if expected_folder_name in breadcrumb_titles:
        return

    joined_titles = "/".join(breadcrumb_titles) if breadcrumb_titles else "<empty>"
    raise RuntimeError(
        "Loaded folder does not match expected path segment "
        f"'{expected_folder_name}'. Current breadcrumb: {joined_titles}"
    )


def _load_folder_with_retry(
    page: Page,
    *,
    target_url: str,
    timeout_ms: int,
    allow_interactive_login: bool,
    expected_folder_name: str | None,
) -> None:
    deadline = time.monotonic() + (FOLDER_LOAD_RETRY_TIMEOUT_MS / 1000)
    last_error: Exception = RuntimeError("Folder load retry exhausted")
    attempt = 1

    while True:
        try:
            _log(
                f"Loading folder attempt {attempt}: {target_url}"
                + (
                    f" (expecting '{expected_folder_name}')"
                    if expected_folder_name is not None
                    else ""
                )
            )
            page.goto(target_url, wait_until="domcontentloaded")
            ensure_authenticated_page(
                page,
                target_url=target_url,
                allow_interactive_login=allow_interactive_login,
            )
            _wait_for_folder_view_settle(page, timeout_ms)
            _ensure_expected_folder_loaded(page, expected_folder_name)
            _log(f"Folder load succeeded on attempt {attempt}: {target_url}")
            return
        except Exception as error:
            last_error = error
            _log(f"Folder load attempt {attempt} failed: {error}")

        if time.monotonic() >= deadline:
            break

        attempt += 1
        _log(
            f"Retrying folder load in {FOLDER_LOAD_RETRY_INTERVAL_MS // 1000}s: {target_url}"
        )
        page.wait_for_timeout(FOLDER_LOAD_RETRY_INTERVAL_MS)

    raise RuntimeError(
        "Failed to load folder after retries "
        f"({FOLDER_LOAD_RETRY_TIMEOUT_MS // 1000}s limit): {last_error}"
    )


def _load_folder_entries_with_retry(
    page: Page,
    *,
    downloads_dir: Path,
    target_url: str,
    timeout_ms: int,
    allow_interactive_login: bool,
    expected_folder_name: str | None,
    use_folder_cache: bool,
) -> RemoteEntries:
    if use_folder_cache:
        cached_entries = load_folder_entries_cache(downloads_dir, target_url)
        if cached_entries is not None:
            _log(
                "Using cached folder entries: "
                f"{target_url} ({len(cached_entries.files)} file(s), "
                f"{len(cached_entries.folders)} folder(s))"
            )
            return cached_entries

    deadline = time.monotonic() + (FOLDER_LOAD_RETRY_TIMEOUT_MS / 1000)
    last_error: Exception = RuntimeError("Folder entries retry exhausted")
    attempt = 1

    while True:
        try:
            _load_folder_with_retry(
                page,
                target_url=target_url,
                timeout_ms=timeout_ms,
                allow_interactive_login=allow_interactive_login,
                expected_folder_name=expected_folder_name,
            )
            entries = _evaluate_current_folder_entries(page)
            _log(
                f"Folder entries attempt {attempt}: found {len(entries.files)} file(s), "
                f"{len(entries.folders)} folder(s)"
            )
            write_folder_entries_cache(downloads_dir, target_url, entries)

            # Child folders that parse as empty right after navigation are
            # usually not fully rendered yet in IDrive's SPA flow.
            if (
                expected_folder_name is not None
                and not entries.files
                and not entries.folders
            ):
                raise RuntimeError(
                    "Folder entries still empty after load; waiting and retrying."
                )

            return entries
        except Exception as error:
            last_error = error
            _log(f"Folder entries attempt {attempt} failed: {error}")

        if time.monotonic() >= deadline:
            break

        attempt += 1
        _log(
            f"Retrying folder entries in {FOLDER_LOAD_RETRY_INTERVAL_MS // 1000}s: {target_url}"
        )
        page.wait_for_timeout(FOLDER_LOAD_RETRY_INTERVAL_MS)

    raise RuntimeError(
        "Failed to extract folder entries after retries "
        f"({FOLDER_LOAD_RETRY_TIMEOUT_MS // 1000}s limit): {last_error}"
    )


def _download_one_file(
    page: Page,
    remote_file: RemoteFile,
    staging_dir: Path,
    cooldown_ms: int,
) -> Path:
    script = _load_js_asset("trigger_file_download.js")
    _log(f"Starting download: {remote_file.file_name}")

    try:
        with page.expect_download() as download_info:
            trigger_result: object = page.evaluate(
                script,
                {
                    "fileName": remote_file.file_name,
                    "cooldownMs": cooldown_ms,
                },
            )

        _ensure_trigger_result(trigger_result, remote_file.file_name)
        download = download_info.value
    except PlaywrightTimeoutError as error:
        raise RuntimeError(
            f"Timed out waiting for download: {remote_file.file_name}"
        ) from error
    except PlaywrightError as error:
        raise RuntimeError(
            f"Download canceled by browser/session: {remote_file.file_name} ({error})"
        ) from error

    staged_path = staging_dir / download.suggested_filename
    try:
        download.save_as(str(staged_path))
    except PlaywrightError as error:
        raise RuntimeError(
            f"Failed saving download: {remote_file.file_name} ({error})"
        ) from error
    _log(f"Staged download complete: {remote_file.file_name} -> {staged_path}")
    return staged_path


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


def _relative_path_from_destination(base_destination: Path, final_path: Path) -> str:
    try:
        return str(final_path.relative_to(base_destination))
    except ValueError:
        return final_path.name


def _write_manifest(report: DownloadFolderReport, repo_root: Path) -> None:
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
                "relativePath": _relative_path_from_destination(
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


def _precheck_overwrite_conflicts(
    remote_files: list[RemoteFile],
    destination: Path,
    overwrite: OverwriteMode,
) -> None:
    if overwrite != "fail":
        return

    conflicting_files = [
        remote_file.file_name
        for remote_file in remote_files
        if (destination / remote_file.file_name).exists()
    ]
    if conflicting_files:
        joined_names = ", ".join(conflicting_files)
        raise RuntimeError(f"Destination already contains files: {joined_names}")


def list_current_folder_files(
    *,
    profile_dir: Path,
    downloads_dir: Path,
    url: str,
    headless: bool,
    timeout_ms: int,
) -> list[RemoteFile]:
    config = BrowserConfig(
        profile_dir=profile_dir,
        downloads_dir=downloads_dir,
        headless=headless,
        timeout_ms=timeout_ms,
    )

    with BrowserEngine(config) as engine:
        page = engine.new_page()
        entries = _load_folder_entries_with_retry(
            page,
            downloads_dir=downloads_dir,
            target_url=url,
            timeout_ms=timeout_ms,
            allow_interactive_login=not headless,
            expected_folder_name=None,
            use_folder_cache=False,
        )
        return entries.files


def download_current_folder(
    *,
    profile_dir: Path,
    downloads_dir: Path,
    url: str,
    destination: Path,
    headless: bool,
    timeout_ms: int,
    cooldown_ms: int,
    overwrite: str,
    use_folder_cache: bool = True,
    resume_from_logs: bool = True,
) -> DownloadFolderReport:
    overwrite_mode = cast(OverwriteMode, overwrite)
    destination = ensure_destination_dir(destination)
    config = BrowserConfig(
        profile_dir=profile_dir,
        downloads_dir=downloads_dir,
        headless=headless,
        timeout_ms=timeout_ms,
    )
    started_at = datetime.now()
    progress_log_path = build_progress_log_path(
        downloads_dir,
        started_at,
        prefix="download-folder-progress",
    )
    progress_logger = ProgressEventLogger(progress_log_path)
    progress_logger.log(
        "run_started",
        mode="download-folder",
        url=url,
        destination=str(destination),
        overwrite=overwrite_mode,
        useFolderCache=use_folder_cache,
        resumeFromLogs=resume_from_logs,
    )

    downloaded: list[DownloadedFile] = []
    skipped: list[SkippedFile] = []
    failed: list[FailedFile] = []
    discovered_files: list[ManifestFileRecord] = []
    folder_queue: list[FolderTask] = [
        FolderTask(url=url, destination=destination, expected_folder_name=None)
    ]
    visited_destinations: set[Path] = set()
    successful_from_logs: set[str] = set()

    if resume_from_logs and overwrite_mode == "skip":
        successful_from_logs = load_resume_success_relative_paths(
            downloads_dir,
            url=url,
            destination=destination,
        )
        progress_logger.log(
            "resume_index_loaded",
            successfulCount=len(successful_from_logs),
        )
        if successful_from_logs:
            _log(
                "Resume log index loaded: "
                f"{len(successful_from_logs)} previously successful file(s)"
            )

    try:
        with BrowserEngine(config) as engine:
            page = engine.new_page()
            while folder_queue:
                folder_task = folder_queue.pop(0)
                if folder_task.destination in visited_destinations:
                    _log(
                        f"Skipping already visited destination: {folder_task.destination}"
                    )
                    progress_logger.log(
                        "folder_skipped_visited",
                        folderUrl=folder_task.url,
                        destination=str(folder_task.destination),
                    )
                    continue

                visited_destinations.add(folder_task.destination)
                ensure_destination_dir(folder_task.destination)
                _log(
                    f"Processing folder: {folder_task.url} -> {folder_task.destination} "
                    f"(queue remaining: {len(folder_queue)})"
                )
                progress_logger.log(
                    "folder_started",
                    folderUrl=folder_task.url,
                    destination=str(folder_task.destination),
                    queueRemaining=len(folder_queue),
                )

                remote_entries = _load_folder_entries_with_retry(
                    page,
                    downloads_dir=downloads_dir,
                    target_url=folder_task.url,
                    timeout_ms=timeout_ms,
                    allow_interactive_login=not headless,
                    expected_folder_name=folder_task.expected_folder_name,
                    use_folder_cache=use_folder_cache,
                )
                progress_logger.log(
                    "folder_entries_loaded",
                    folderUrl=folder_task.url,
                    fileCount=len(remote_entries.files),
                    folderCount=len(remote_entries.folders),
                )
                _precheck_overwrite_conflicts(
                    remote_entries.files,
                    folder_task.destination,
                    overwrite_mode,
                )

                for remote_folder in remote_entries.folders:
                    child_destination = (
                        folder_task.destination / remote_folder.folder_name
                    )
                    _log(
                        "Queueing child folder: "
                        f"{remote_folder.folder_name} -> {child_destination}"
                    )
                    progress_logger.log(
                        "folder_queued",
                        folderUrl=remote_folder.href,
                        destination=str(child_destination),
                    )
                    folder_queue.append(
                        FolderTask(
                            url=remote_folder.href,
                            destination=child_destination,
                            expected_folder_name=remote_folder.folder_name,
                        )
                    )

                for remote_file in remote_entries.files:
                    final_path = folder_task.destination / remote_file.file_name
                    relative_path = _relative_path_from_destination(
                        destination, final_path
                    )
                    discovered_files.append(
                        ManifestFileRecord(
                            folder_url=folder_task.url,
                            relative_path=relative_path,
                            file_name=remote_file.file_name,
                            final_path=final_path,
                            server_size_text=remote_file.server_size_text,
                            server_modified_text=remote_file.server_modified_text,
                        )
                    )
                    progress_logger.log(
                        "file_discovered",
                        folderUrl=folder_task.url,
                        fileName=remote_file.file_name,
                        relativePath=relative_path,
                    )

                    if relative_path in successful_from_logs:
                        _log(f"Skipping previously successful file: {relative_path}")
                        progress_logger.log(
                            "file_skipped",
                            fileName=remote_file.file_name,
                            relativePath=relative_path,
                            reason="previous run success",
                        )
                        skipped.append(
                            SkippedFile(
                                file_name=remote_file.file_name,
                                reason="previous run success",
                                final_path=final_path,
                            )
                        )
                        continue

                    if final_path.exists() and overwrite_mode == "skip":
                        _log(f"Skipping existing file: {final_path}")
                        progress_logger.log(
                            "file_skipped",
                            fileName=remote_file.file_name,
                            relativePath=relative_path,
                            reason="destination exists",
                        )
                        skipped.append(
                            SkippedFile(
                                file_name=remote_file.file_name,
                                reason="destination exists",
                                final_path=final_path,
                            )
                        )
                        continue

                    progress_logger.log(
                        "file_download_started",
                        fileName=remote_file.file_name,
                        relativePath=relative_path,
                    )
                    try:
                        staged_path = _download_one_file(
                            page,
                            remote_file,
                            downloads_dir,
                            cooldown_ms,
                        )
                        moved_path = move_download_to_destination(
                            staged_path,
                            folder_task.destination,
                            remote_file.file_name,
                            replace_existing=overwrite_mode == "replace",
                        )
                        _log(f"Moved download to destination: {moved_path}")
                    except (OSError, RuntimeError) as error:
                        _log(f"Failed file download: {remote_file.file_name} ({error})")
                        progress_logger.log(
                            "file_failed",
                            fileName=remote_file.file_name,
                            relativePath=relative_path,
                            reason=str(error),
                        )
                        failed.append(
                            FailedFile(
                                file_name=remote_file.file_name,
                                reason=str(error),
                                final_path=final_path,
                            )
                        )
                        continue

                    progress_logger.log(
                        "file_downloaded",
                        fileName=remote_file.file_name,
                        relativePath=relative_path,
                        stagedPath=str(staged_path),
                        finalPath=str(moved_path),
                    )
                    downloaded.append(
                        DownloadedFile(
                            file_name=remote_file.file_name,
                            staged_path=staged_path,
                            final_path=moved_path,
                        )
                    )
    except Exception as error:
        progress_logger.log("run_failed", reason=str(error))
        raise

    finished_at = datetime.now()
    repo_root = downloads_dir.parents[2]
    manifest_path = build_manifest_path(downloads_dir, started_at)
    report = DownloadFolderReport(
        url=url,
        destination=destination,
        started_at=started_at,
        finished_at=finished_at,
        downloaded=downloaded,
        skipped=skipped,
        failed=failed,
        discovered_files=discovered_files,
        manifest_path=manifest_path,
        progress_log_path=progress_log_path,
    )
    progress_logger.log(
        "run_finished",
        downloadedCount=len(downloaded),
        skippedCount=len(skipped),
        failedCount=len(failed),
        manifestPath=str(manifest_path),
        exitCode=report.exit_code,
    )
    _write_manifest(report, repo_root)
    return report


def retry_missing_files_from_manifest(
    *,
    profile_dir: Path,
    downloads_dir: Path,
    manifest_path: Path,
    headless: bool,
    timeout_ms: int,
    cooldown_ms: int,
    overwrite: str,
) -> DownloadFolderReport:
    manifest = load_download_manifest(manifest_path)
    targets = [
        item for item in manifest.discovered_files if not item.final_path.exists()
    ]
    overwrite_mode = cast(OverwriteMode, overwrite)
    started_at = datetime.now()
    progress_log_path = build_progress_log_path(
        downloads_dir,
        started_at,
        prefix="retry-manifest-progress",
    )
    progress_logger = ProgressEventLogger(progress_log_path)
    progress_logger.log(
        "run_started",
        mode="retry-manifest",
        manifestPath=str(manifest_path),
        targetCount=len(targets),
        overwrite=overwrite_mode,
    )

    downloaded: list[DownloadedFile] = []
    skipped: list[SkippedFile] = []
    failed: list[FailedFile] = []

    if not targets:
        report = DownloadFolderReport(
            url=manifest.url,
            destination=manifest.destination,
            started_at=started_at,
            finished_at=datetime.now(),
            downloaded=[],
            skipped=[],
            failed=[],
            discovered_files=[],
            manifest_path=build_retry_manifest_path(downloads_dir, started_at),
            progress_log_path=progress_log_path,
        )
        progress_logger.log(
            "run_finished",
            downloadedCount=0,
            skippedCount=0,
            failedCount=0,
            manifestPath=str(report.manifest_path),
            exitCode=report.exit_code,
        )
        _write_manifest(report, downloads_dir.parents[2])
        return report

    grouped_targets: dict[tuple[str, Path], list[ManifestFileRecord]] = {}
    for item in targets:
        key = (item.folder_url, item.final_path.parent)
        grouped_targets.setdefault(key, []).append(item)

    config = BrowserConfig(
        profile_dir=profile_dir,
        downloads_dir=downloads_dir,
        headless=headless,
        timeout_ms=timeout_ms,
    )

    try:
        with BrowserEngine(config) as engine:
            page = engine.new_page()
            for (folder_url, destination_dir), grouped_items in grouped_targets.items():
                _log(f"Retrying {len(grouped_items)} file(s) from folder: {folder_url}")
                progress_logger.log(
                    "folder_started",
                    folderUrl=folder_url,
                    destination=str(destination_dir),
                    targetCount=len(grouped_items),
                )
                remote_entries = _load_folder_entries_with_retry(
                    page,
                    downloads_dir=downloads_dir,
                    target_url=folder_url,
                    timeout_ms=timeout_ms,
                    allow_interactive_login=not headless,
                    expected_folder_name=None,
                    use_folder_cache=True,
                )
                available_files = {item.file_name for item in remote_entries.files}

                for item in grouped_items:
                    progress_logger.log(
                        "file_discovered",
                        folderUrl=folder_url,
                        fileName=item.file_name,
                        relativePath=item.relative_path,
                    )
                    if item.final_path.exists() and overwrite_mode == "skip":
                        progress_logger.log(
                            "file_skipped",
                            fileName=item.file_name,
                            relativePath=item.relative_path,
                            reason="destination exists",
                        )
                        skipped.append(
                            SkippedFile(
                                file_name=item.file_name,
                                reason="destination exists",
                                final_path=item.final_path,
                            )
                        )
                        continue

                    if item.file_name not in available_files:
                        reason = "File no longer present in remote folder view"
                        progress_logger.log(
                            "file_failed",
                            fileName=item.file_name,
                            relativePath=item.relative_path,
                            reason=reason,
                        )
                        failed.append(
                            FailedFile(
                                file_name=item.file_name,
                                reason=reason,
                                final_path=item.final_path,
                            )
                        )
                        continue

                    progress_logger.log(
                        "file_download_started",
                        fileName=item.file_name,
                        relativePath=item.relative_path,
                    )
                    try:
                        staged_path = _download_one_file(
                            page,
                            RemoteFile(
                                file_name=item.file_name,
                                row_index=-1,
                                server_size_text=item.server_size_text,
                                server_modified_text=item.server_modified_text,
                            ),
                            downloads_dir,
                            cooldown_ms,
                        )
                        moved_path = move_download_to_destination(
                            staged_path,
                            destination_dir,
                            item.file_name,
                            replace_existing=overwrite_mode == "replace",
                        )
                    except (OSError, RuntimeError) as error:
                        progress_logger.log(
                            "file_failed",
                            fileName=item.file_name,
                            relativePath=item.relative_path,
                            reason=str(error),
                        )
                        failed.append(
                            FailedFile(
                                file_name=item.file_name,
                                reason=str(error),
                                final_path=item.final_path,
                            )
                        )
                        continue

                    progress_logger.log(
                        "file_downloaded",
                        fileName=item.file_name,
                        relativePath=item.relative_path,
                        stagedPath=str(staged_path),
                        finalPath=str(moved_path),
                    )
                    downloaded.append(
                        DownloadedFile(
                            file_name=item.file_name,
                            staged_path=staged_path,
                            final_path=moved_path,
                        )
                    )
    except Exception as error:
        progress_logger.log("run_failed", reason=str(error))
        raise

    report = DownloadFolderReport(
        url=manifest.url,
        destination=manifest.destination,
        started_at=started_at,
        finished_at=datetime.now(),
        downloaded=downloaded,
        skipped=skipped,
        failed=failed,
        discovered_files=targets,
        manifest_path=build_retry_manifest_path(downloads_dir, started_at),
        progress_log_path=progress_log_path,
    )
    progress_logger.log(
        "run_finished",
        downloadedCount=len(downloaded),
        skippedCount=len(skipped),
        failedCount=len(failed),
        manifestPath=str(report.manifest_path),
        exitCode=report.exit_code,
    )
    _write_manifest(report, downloads_dir.parents[2])
    return report
