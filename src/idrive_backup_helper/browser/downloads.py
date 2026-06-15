from datetime import datetime
from pathlib import Path
from typing import cast

from idrive_backup_helper.browser.download_cache import (
    load_folder_entries_cache,
    load_resume_success_relative_paths,
    write_folder_entries_cache,
)
from idrive_backup_helper.browser.download_entries import (
    ensure_raw_file_list,
    parse_remote_entries,
    parse_remote_files,
)
from idrive_backup_helper.browser.download_manifest import (
    build_manifest_path,
    build_retry_manifest_path,
    ensure_destination_dir,
    load_download_manifest,
    relative_path_from_destination,
    verify_download_manifest,
    write_manifest,
)
from idrive_backup_helper.browser.download_models import (
    DownloadFolderReport,
    DownloadManifest,
    DownloadedFile,
    FailedFile,
    FolderTask,
    ManifestFileRecord,
    ManifestVerification,
    OverwriteMode,
    RemoteEntries,
    RemoteFile,
    RemoteFolder,
    SkippedFile,
)
from idrive_backup_helper.browser.download_page import (
    download_one_file,
    load_folder_entries_with_retry,
)
from idrive_backup_helper.browser.download_progress import (
    ProgressEventLogger,
    build_progress_log_path,
    log_download_message,
)
from idrive_backup_helper.browser.engine import BrowserConfig, BrowserEngine
from idrive_backup_helper.filesystem.moves import move_download_to_destination

# Re-export stable surface so callers/tests don't need to import split modules.
__all__ = [
    "DownloadFolderReport",
    "DownloadManifest",
    "DownloadedFile",
    "FailedFile",
    "FolderTask",
    "ManifestFileRecord",
    "ManifestVerification",
    "OverwriteMode",
    "ProgressEventLogger",
    "RemoteEntries",
    "RemoteFile",
    "RemoteFolder",
    "SkippedFile",
    "build_manifest_path",
    "build_progress_log_path",
    "build_retry_manifest_path",
    "download_current_folder",
    "ensure_destination_dir",
    "ensure_raw_file_list",
    "list_current_folder_files",
    "load_download_manifest",
    "load_folder_entries_cache",
    "load_resume_success_relative_paths",
    "parse_remote_entries",
    "parse_remote_files",
    "retry_missing_files_from_manifest",
    "verify_download_manifest",
    "write_folder_entries_cache",
]


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
        entries = load_folder_entries_with_retry(
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
            log_download_message(
                "Resume log index loaded: "
                f"{len(successful_from_logs)} previously successful file(s)"
            )

    try:
        with BrowserEngine(config) as engine:
            page = engine.new_page()
            while folder_queue:
                folder_task = folder_queue.pop(0)
                if folder_task.destination in visited_destinations:
                    log_download_message(
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
                log_download_message(
                    f"Processing folder: {folder_task.url} -> {folder_task.destination} "
                    f"(queue remaining: {len(folder_queue)})"
                )
                progress_logger.log(
                    "folder_started",
                    folderUrl=folder_task.url,
                    destination=str(folder_task.destination),
                    queueRemaining=len(folder_queue),
                )

                remote_entries = load_folder_entries_with_retry(
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
                    log_download_message(
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
                    relative_path = relative_path_from_destination(
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
                        log_download_message(
                            f"Skipping previously successful file: {relative_path}"
                        )
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
                        log_download_message(f"Skipping existing file: {final_path}")
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
                        staged_path = download_one_file(
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
                        log_download_message(
                            f"Moved download to destination: {moved_path}"
                        )
                    except (OSError, RuntimeError) as error:
                        log_download_message(
                            f"Failed file download: {remote_file.file_name} ({error})"
                        )
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
    write_manifest(report, repo_root)
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
        write_manifest(report, downloads_dir.parents[2])
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
                log_download_message(
                    f"Retrying {len(grouped_items)} file(s) from folder: {folder_url}"
                )
                progress_logger.log(
                    "folder_started",
                    folderUrl=folder_url,
                    destination=str(destination_dir),
                    targetCount=len(grouped_items),
                )
                remote_entries = load_folder_entries_with_retry(
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
                        staged_path = download_one_file(
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
    write_manifest(report, downloads_dir.parents[2])
    return report
