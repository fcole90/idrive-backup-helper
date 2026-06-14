from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Literal, cast

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

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


@dataclass(frozen=True)
class FailedFile:
    file_name: str
    reason: str


@dataclass(frozen=True)
class DownloadFolderReport:
    url: str
    destination: Path
    started_at: datetime
    finished_at: datetime
    downloaded: list[DownloadedFile]
    skipped: list[SkippedFile]
    failed: list[FailedFile]
    manifest_path: Path

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0


type OverwriteMode = Literal["skip", "replace", "fail"]


@dataclass(frozen=True)
class FolderTask:
    url: str
    destination: Path


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


def _evaluate_current_folder_files(page: Page) -> list[RemoteFile]:
    return _evaluate_current_folder_entries(page).files


def _download_one_file(
    page: Page,
    remote_file: RemoteFile,
    staging_dir: Path,
    cooldown_ms: int,
) -> Path:
    script = _load_js_asset("trigger_file_download.js")

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

    staged_path = staging_dir / download.suggested_filename
    download.save_as(str(staged_path))
    return staged_path


def build_manifest_path(downloads_dir: Path, started_at: datetime) -> Path:
    timestamp = started_at.strftime("%Y-%m-%dT%H-%M-%S")
    return downloads_dir / f"download-folder-run-{timestamp}.json"


def ensure_destination_dir(destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    return destination


def _serialize_path(path: Path, repo_root: Path) -> str:
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path)


def _write_manifest(report: DownloadFolderReport, repo_root: Path) -> None:
    manifest = {
        "version": "poc-download-folder-1.0",
        "url": report.url,
        "destination": str(report.destination),
        "startedAt": report.started_at.isoformat(timespec="seconds"),
        "finishedAt": report.finished_at.isoformat(timespec="seconds"),
        "downloaded": [
            {
                "fileName": item.file_name,
                "stagedPath": _serialize_path(item.staged_path, repo_root),
                "finalPath": str(item.final_path),
            }
            for item in report.downloaded
        ],
        "skipped": [
            {"fileName": item.file_name, "reason": item.reason}
            for item in report.skipped
        ],
        "failed": [
            {"fileName": item.file_name, "reason": item.reason}
            for item in report.failed
        ],
    }
    report.manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
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
        page.goto(url, wait_until="domcontentloaded")
        ensure_authenticated_page(
            page,
            target_url=url,
            allow_interactive_login=not headless,
        )
        return _evaluate_current_folder_files(page)


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
    downloaded: list[DownloadedFile] = []
    skipped: list[SkippedFile] = []
    failed: list[FailedFile] = []
    folder_queue: list[FolderTask] = [FolderTask(url=url, destination=destination)]
    visited_urls: set[str] = set()

    with BrowserEngine(config) as engine:
        page = engine.new_page()
        while folder_queue:
            folder_task = folder_queue.pop(0)
            if folder_task.url in visited_urls:
                continue

            visited_urls.add(folder_task.url)
            ensure_destination_dir(folder_task.destination)

            page.goto(folder_task.url, wait_until="domcontentloaded")
            ensure_authenticated_page(
                page,
                target_url=folder_task.url,
                allow_interactive_login=not headless,
            )
            remote_entries = _evaluate_current_folder_entries(page)
            _precheck_overwrite_conflicts(
                remote_entries.files,
                folder_task.destination,
                overwrite_mode,
            )

            for remote_folder in remote_entries.folders:
                child_destination = folder_task.destination / remote_folder.folder_name
                folder_queue.append(
                    FolderTask(
                        url=remote_folder.href,
                        destination=child_destination,
                    )
                )

            for remote_file in remote_entries.files:
                final_path = folder_task.destination / remote_file.file_name
                if final_path.exists() and overwrite_mode == "skip":
                    skipped.append(
                        SkippedFile(
                            file_name=remote_file.file_name,
                            reason="destination exists",
                        )
                    )
                    continue

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
                except (OSError, RuntimeError) as error:
                    failed.append(
                        FailedFile(
                            file_name=remote_file.file_name,
                            reason=str(error),
                        )
                    )
                    continue

                downloaded.append(
                    DownloadedFile(
                        file_name=remote_file.file_name,
                        staged_path=staged_path,
                        final_path=moved_path,
                    )
                )

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
        manifest_path=manifest_path,
    )
    _write_manifest(report, repo_root)
    return report
