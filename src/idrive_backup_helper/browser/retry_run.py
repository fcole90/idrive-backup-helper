from datetime import datetime
from pathlib import Path
from typing import cast

from idrive_backup_helper.browser.downloads.download_manifest import (
    StreamingManifestWriter,
    build_retry_manifest_path,
    iter_manifest_discovered_files,
    read_manifest_header,
)
from idrive_backup_helper.browser.downloads.download_models import (
    DownloadFolderReport,
    FailedFile,
    ManifestFileRecord,
    ManifestHeader,
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
from idrive_backup_helper.filesystem.listing import DirectoryListingCache
from idrive_backup_helper.filesystem.moves import clear_staging_dir
from idrive_backup_helper.filesystem.paths import staging_dir_for_destination


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
    header = read_manifest_header(manifest_path)
    overwrite_mode = cast(OverwriteMode, overwrite)
    started_at = datetime.now()
    progress_log_path = build_progress_log_path(
        downloads_dir,
        started_at,
        prefix="retry-manifest-progress",
    )
    progress_logger = ProgressEventLogger(progress_log_path)

    # Stream the manifest inventory and keep only the still-missing files, grouped
    # by folder. One scandir per parent directory (DirectoryListingCache) instead of
    # a stat per discovered file; the manifest can list a very large tree and
    # per-file stats are ~1s on slow destinations (external USB, network mounts).
    # Memory stays O(missing files) rather than O(whole manifest).
    startup_listings = DirectoryListingCache()
    grouped_targets: dict[tuple[str, Path], list[ManifestFileRecord]] = {}
    target_count = 0
    for item in iter_manifest_discovered_files(manifest_path):
        if startup_listings.contains(item.final_path):
            continue
        grouped_targets.setdefault(
            (item.folder_url, item.final_path.parent), []
        ).append(item)
        target_count += 1

    retry_manifest_path = build_retry_manifest_path(downloads_dir, started_at)
    repo_root = downloads_dir.parents[2]
    manifest_writer = StreamingManifestWriter(
        manifest_path=retry_manifest_path,
        repo_root=repo_root,
        url=header.url,
        destination=header.destination,
        started_at=started_at,
        progress_log_path=progress_log_path,
    )
    progress_logger.log(
        "run_started",
        mode="retry-manifest",
        manifestPath=str(manifest_path),
        targetCount=target_count,
        overwrite=overwrite_mode,
    )

    if not grouped_targets:
        return _finish(
            manifest_writer=manifest_writer,
            progress_logger=progress_logger,
            header=header,
            started_at=started_at,
            progress_log_path=progress_log_path,
        )

    staging_dir = staging_dir_for_destination(header.destination)
    cleared_staging = clear_staging_dir(staging_dir)
    if cleared_staging:
        log_download_message(
            f"Cleared {len(cleared_staging)} leftover staging file(s) in {staging_dir}"
        )
    config = BrowserConfig(
        profile_dir=profile_dir,
        staging_dir=staging_dir,
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
                    manifest_writer.record_discovered(item)
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
                        manifest_writer.record_skipped(
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
                        manifest_writer.record_failed(
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
                            staging_dir=staging_dir,
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
                        manifest_writer.record_failed(
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
                    manifest_writer.record_downloaded(downloaded_file)
    except Exception as error:
        progress_logger.log("run_failed", reason=str(error))
        # Leave the partial journal on disk; do not publish a final manifest.
        manifest_writer.close()
        raise

    return _finish(
        manifest_writer=manifest_writer,
        progress_logger=progress_logger,
        header=header,
        started_at=started_at,
        progress_log_path=progress_log_path,
    )


def _finish(
    *,
    manifest_writer: StreamingManifestWriter,
    progress_logger: ProgressEventLogger,
    header: ManifestHeader,
    started_at: datetime,
    progress_log_path: Path,
) -> DownloadFolderReport:
    finished_at = datetime.now()
    counts = manifest_writer.finalize(finished_at)
    report = DownloadFolderReport(
        url=header.url,
        destination=header.destination,
        started_at=started_at,
        finished_at=finished_at,
        counts=counts,
        manifest_path=manifest_writer.manifest_path,
        progress_log_path=progress_log_path,
    )
    progress_logger.log(
        "run_finished",
        downloadedCount=counts.downloaded,
        skippedCount=counts.skipped,
        failedCount=counts.failed,
        manifestPath=str(manifest_writer.manifest_path),
        exitCode=report.exit_code,
    )
    return report
