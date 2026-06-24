from datetime import datetime
from pathlib import Path
from typing import cast

from idrive_backup_helper.browser.downloads.download_cache import (
    load_resume_success_relative_paths,
)
from idrive_backup_helper.browser.downloads.download_manifest import (
    build_manifest_path,
    ensure_destination_dir,
    relative_path_from_destination,
    write_manifest,
)
from idrive_backup_helper.browser.downloads.download_models import (
    DownloadFolderReport,
    DownloadedFile,
    FailedFile,
    FolderTask,
    ManifestFileRecord,
    OverwriteMode,
    RemoteFile,
    SkippedFile,
)
from idrive_backup_helper.browser.downloads.download_page import (
    ensure_folder_loaded_for_download,
    load_folder_entries_with_retry,
)
from idrive_backup_helper.browser.downloads.download_progress import (
    ProgressEventLogger,
    build_progress_log_path,
    log_download_message,
)
from idrive_backup_helper.browser.downloads.download_transfer import (
    transfer_remote_file_to_destination,
)
from idrive_backup_helper.browser.engine import BrowserConfig, BrowserEngine
from idrive_backup_helper.filesystem.listing import existing_entry_names


def _precheck_overwrite_conflicts(
    remote_files: list[RemoteFile],
    existing_names: set[str],
    overwrite: OverwriteMode,
) -> None:
    if overwrite != "fail":
        return

    conflicting_files = [
        remote_file.file_name
        for remote_file in remote_files
        if remote_file.file_name in existing_names
    ]
    if conflicting_files:
        joined_names = ", ".join(conflicting_files)
        raise RuntimeError(f"Destination already contains files: {joined_names}")


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
    browser_debug_url: str | None = None,
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
        browser_debug_url=browser_debug_url,
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
            page = engine.current_page_or_new_page()
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
                # One scandir of this folder's destination replaces a per-file
                # stat. Existence checks dominate resume runs and a stat per file
                # is ~1s on slow destinations (external USB, network mounts).
                folder_existing_names = existing_entry_names(folder_task.destination)
                _precheck_overwrite_conflicts(
                    remote_entries.files,
                    folder_existing_names,
                    overwrite_mode,
                )
                folder_page_loaded_for_download = False

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

                # Partition pass: decide skip-vs-download for every file with no
                # per-file printing or progress write. On a fully covered folder
                # the old per-file print + ndjson write cost ~100-200ms each over a
                # slow terminal, so this is summarized in a single line per folder.
                files_to_download: list[tuple[RemoteFile, Path, str]] = []
                skipped_in_folder = 0
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

                    if (
                        overwrite_mode == "skip"
                        and remote_file.file_name in folder_existing_names
                    ):
                        reason = (
                            "previous run success"
                            if relative_path in successful_from_logs
                            else "destination exists"
                        )
                        skipped.append(
                            SkippedFile(
                                file_name=remote_file.file_name,
                                reason=reason,
                                final_path=final_path,
                            )
                        )
                        skipped_in_folder += 1
                        continue

                    files_to_download.append((remote_file, final_path, relative_path))

                progress_logger.log(
                    "folder_files_partitioned",
                    folderUrl=folder_task.url,
                    skippedExisting=skipped_in_folder,
                    toDownload=len(files_to_download),
                )
                if not files_to_download:
                    log_download_message(
                        f"All {skipped_in_folder} file(s) already present in "
                        f"{folder_task.destination}; nothing to download"
                    )
                else:
                    log_download_message(
                        f"{skipped_in_folder} file(s) already present, "
                        f"{len(files_to_download)} to download in "
                        f"{folder_task.destination}"
                    )

                for remote_file, final_path, relative_path in files_to_download:
                    if not folder_page_loaded_for_download:
                        log_download_message(
                            "Loading folder page before first download attempt: "
                            f"{folder_task.url}"
                        )
                        ensure_folder_loaded_for_download(
                            page,
                            target_url=folder_task.url,
                            timeout_ms=timeout_ms,
                            allow_interactive_login=not headless,
                            expected_folder_name=folder_task.expected_folder_name,
                        )
                        folder_page_loaded_for_download = True

                    log_download_message(
                        "Attempting IDrive download for remote file: "
                        f"{relative_path}"
                    )
                    progress_logger.log(
                        "file_download_started",
                        fileName=remote_file.file_name,
                        relativePath=relative_path,
                    )
                    try:
                        downloaded_file = transfer_remote_file_to_destination(
                            page=page,
                            remote_file=remote_file,
                            downloads_dir=downloads_dir,
                            destination_dir=folder_task.destination,
                            replace_existing=overwrite_mode == "replace",
                            cooldown_ms=cooldown_ms,
                        )
                        log_download_message(
                            "Moved download to destination: "
                            f"{downloaded_file.final_path}"
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
                        stagedPath=str(downloaded_file.staged_path),
                        finalPath=str(downloaded_file.final_path),
                    )
                    downloaded.append(downloaded_file)
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
