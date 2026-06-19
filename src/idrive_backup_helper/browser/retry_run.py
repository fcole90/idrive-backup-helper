from datetime import datetime
from pathlib import Path
from typing import cast

from idrive_backup_helper.browser.downloads.download_manifest import (
    build_retry_manifest_path,
    load_download_manifest,
    write_manifest,
)
from idrive_backup_helper.browser.downloads.download_models import (
    DownloadFolderReport,
    DownloadedFile,
    FailedFile,
    ManifestFileRecord,
    OverwriteMode,
    RemoteFile,
    SkippedFile,
)
from idrive_backup_helper.browser.downloads.download_page import (
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


def retry_missing_files_from_manifest(
    *,
    profile_dir: Path,
    downloads_dir: Path,
    manifest_path: Path,
    headless: bool,
    timeout_ms: int,
    cooldown_ms: int,
    overwrite: str,
    browser_debug_url: str | None = None,
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
        browser_debug_url=browser_debug_url,
    )

    try:
        with BrowserEngine(config) as engine:
            page = engine.current_page_or_new_page()
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
                        downloaded_file = transfer_remote_file_to_destination(
                            page=page,
                            remote_file=RemoteFile(
                                file_name=item.file_name,
                                row_index=-1,
                                server_size_text=item.server_size_text,
                                server_modified_text=item.server_modified_text,
                            ),
                            downloads_dir=downloads_dir,
                            destination_dir=destination_dir,
                            replace_existing=overwrite_mode == "replace",
                            cooldown_ms=cooldown_ms,
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
                        stagedPath=str(downloaded_file.staged_path),
                        finalPath=str(downloaded_file.final_path),
                    )
                    downloaded.append(downloaded_file)
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
