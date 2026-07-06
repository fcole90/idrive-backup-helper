from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
import time
from typing import cast

from playwright.sync_api import Page

from idrive_backup_helper.browser.downloads.download_cache import (
    load_resume_success_relative_paths,
)
from idrive_backup_helper.browser.downloads.download_diagnostics import (
    BrowserCrashContext,
    browser_cmdline_markers,
    capture_resource_snapshot,
    render_crash_report,
)
from idrive_backup_helper.browser.downloads.download_manifest import (
    StreamingManifestWriter,
    build_manifest_path,
    ensure_destination_dir,
    relative_path_from_destination,
)
from idrive_backup_helper.browser.downloads.download_models import (
    DownloadFolderReport,
    FailedFile,
    FolderTask,
    ManifestFileRecord,
    OverwriteMode,
    RemoteFile,
    SkippedFile,
)
from idrive_backup_helper.browser.downloads.download_page import (
    BrowserClosedError,
    FolderUnavailableError,
    ensure_folder_loaded_for_download,
    idrive_home_url,
    load_folder_entries_with_retry,
)
from idrive_backup_helper.browser.downloads.folder_urls import is_idrive_url
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
from idrive_backup_helper.filesystem.moves import clear_staging_dir
from idrive_backup_helper.filesystem.paths import staging_dir_for_destination


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


# A tab death under memory pressure (renderer OOM/hang kill, Memory Saver
# discard) is recoverable while the browser itself stays alive: reopen a page and
# retry the folder. The cap keeps a folder that reliably kills its renderer from
# looping the run forever; every death still writes a crash-diagnostics report.
TAB_DEATH_RECOVERY_LIMIT = 5
# UI-click navigation never tears down IDrive's SPA, so its renderer heap grows
# for the whole run (6.4h before the 2026-07-05 tab death). A hard goto back to
# home every N folders destroys the document and resets the heap; the next
# folder load re-plans from home via the normal click path.
PAGE_RECYCLE_FOLDER_INTERVAL = 50
# Resource samples are throttled so cache-driven resume runs (many folders per
# second) do not pay a process scan per folder.
RESOURCE_SAMPLE_MIN_INTERVAL_SECONDS = 30.0


def _capture_browser_crash_diagnostics(
    *,
    engine: BrowserEngine,
    downloads_dir: Path,
    run_started_monotonic: float,
    folders_processed: int,
    current_folder: FolderTask,
    manifest_writer: StreamingManifestWriter,
    progress_logger: ProgressEventLogger,
    error: BrowserClosedError,
    cmdline_markers: Sequence[str],
    death_number: int,
) -> None:
    # Fully best-effort: diagnostics must never mask or replace the tab-death
    # handling they describe, so any failure here is logged and swallowed.
    try:
        counts = manifest_writer.counts
        context = BrowserCrashContext(
            error=str(error),
            elapsed_seconds=time.monotonic() - run_started_monotonic,
            folders_processed=folders_processed,
            current_folder_url=current_folder.url,
            current_folder_destination=str(current_folder.destination),
            discovered=counts.discovered,
            downloaded=counts.downloaded,
            skipped=counts.skipped,
            failed=counts.failed,
        )
        health = engine.describe_browser_health()
        resources = capture_resource_snapshot(cmdline_markers)
        captured_at = datetime.now()
        report_text = render_crash_report(
            context=context,
            health=health,
            resources=resources,
            captured_at=captured_at,
        )
        # Named by death time plus death number: several deaths in one run (each
        # possibly recovered) each keep their own report, even within one second.
        timestamp = captured_at.strftime("%Y-%m-%dT%H-%M-%S")
        report_path = (
            downloads_dir / f"download-folder-crash-{timestamp}-{death_number}.md"
        )
        report_path.write_text(report_text, encoding="utf-8")
        log_download_message(
            f"Browser tab died mid-run; wrote crash diagnostics: {report_path}"
        )
        progress_logger.log(
            "browser_crash_diagnostics",
            reportPath=str(report_path),
            browserMode=health.mode,
            cdpReachable=health.cdp_reachable,
            detachedExitCode=health.detached_exit_code,
            elapsedSeconds=round(context.elapsed_seconds, 1),
            foldersProcessed=folders_processed,
            processRssMb=(round(resources.process_rss_mb, 1) if resources else None),
            browserRssMb=(
                round(resources.browser_rss_mb, 1)
                if resources and resources.browser_rss_mb is not None
                else None
            ),
            systemAvailableMb=(
                round(resources.system_available_mb, 1) if resources else None
            ),
        )
    except Exception as diagnostics_error:
        log_download_message(
            f"Failed to capture browser-crash diagnostics: {diagnostics_error}"
        )


def _reopen_page_after_tab_death(engine: BrowserEngine, dead_page: Page) -> Page | None:
    # Opening a page doubles as the browser liveness test: if the whole browser
    # (or its CDP connection) is gone this raises and the caller aborts; if only
    # the tab/renderer died (OOM kill, Memory Saver discard) it succeeds and the
    # run continues on the fresh page.
    try:
        dead_page.close()
    except Exception:
        pass  # Usually already gone; a close failure changes nothing.
    try:
        page = engine.new_page()
    except Exception as reopen_error:
        log_download_message(f"Could not reopen a page after tab death: {reopen_error}")
        return None
    log_download_message("Reopened a fresh page after tab death; resuming the run")
    return page


def _recycle_page_renderer(
    page: Page, *, home_url: str, progress_logger: ProgressEventLogger
) -> None:
    # A hard navigation destroys the SPA document and frees its renderer heap,
    # which UI-click navigation otherwise never releases. Best-effort: on failure
    # the next folder load's own retries surface any real problem.
    try:
        log_download_message(
            f"Recycling the folder page renderer via {home_url} "
            f"(every {PAGE_RECYCLE_FOLDER_INTERVAL} folder(s))"
        )
        page.goto(home_url, wait_until="domcontentloaded")
        progress_logger.log("page_recycled", homeUrl=home_url)
    except Exception as recycle_error:
        log_download_message(
            f"Page recycle failed; continuing without it: {recycle_error}"
        )


def _log_resource_sample(
    progress_logger: ProgressEventLogger, cmdline_markers: Sequence[str]
) -> None:
    # One sample per (throttled) folder turns the crash report's point-in-time
    # numbers into a growth curve across the run.
    snapshot = capture_resource_snapshot(cmdline_markers)
    if snapshot is None:
        return
    progress_logger.log(
        "resource_sample",
        processRssMb=round(snapshot.process_rss_mb, 1),
        browserRssMb=(
            round(snapshot.browser_rss_mb, 1)
            if snapshot.browser_rss_mb is not None
            else None
        ),
        browserProcessCount=snapshot.browser_process_count,
        systemAvailableMb=round(snapshot.system_available_mb, 1),
        systemUsedPercent=snapshot.system_used_percent,
    )


def _process_folder_task(
    *,
    page: Page,
    folder_task: FolderTask,
    folder_queue: list[FolderTask],
    base_destination: Path,
    staging_dir: Path,
    downloads_dir: Path,
    overwrite_mode: OverwriteMode,
    successful_from_logs: set[str],
    manifest_writer: StreamingManifestWriter,
    progress_logger: ProgressEventLogger,
    timeout_ms: int,
    cooldown_ms: int,
    headless: bool,
    use_folder_cache: bool,
) -> None:
    """List one folder, queue its children, and download its missing files.

    Raises ``BrowserClosedError`` if the page dies mid-folder so the caller can
    attempt recovery on a fresh page (or abort when the browser is truly gone).
    """
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
    # One scandir of this folder's destination replaces a per-file stat.
    # Existence checks dominate resume runs and a stat per file is ~1s on slow
    # destinations (external USB, network mounts).
    folder_existing_names = existing_entry_names(folder_task.destination)
    _precheck_overwrite_conflicts(
        remote_entries.files,
        folder_existing_names,
        overwrite_mode,
    )
    folder_page_loaded_for_download = False

    for remote_folder in remote_entries.folders:
        child_destination = folder_task.destination / remote_folder.folder_name
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

    # Partition pass: decide skip-vs-download for every file with no per-file
    # printing or progress write. On a fully covered folder the old per-file
    # print + ndjson write cost ~100-200ms each over a slow terminal, so this is
    # summarized in a single line per folder.
    files_to_download: list[tuple[RemoteFile, Path, str]] = []
    skipped_in_folder = 0
    for remote_file in remote_entries.files:
        final_path = folder_task.destination / remote_file.file_name
        relative_path = relative_path_from_destination(base_destination, final_path)
        manifest_writer.record_discovered(
            ManifestFileRecord(
                folder_url=folder_task.url,
                relative_path=relative_path,
                file_name=remote_file.file_name,
                final_path=final_path,
                server_size_text=remote_file.server_size_text,
                server_modified_text=remote_file.server_modified_text,
            )
        )

        if overwrite_mode == "skip" and remote_file.file_name in folder_existing_names:
            reason = (
                "previous run success"
                if relative_path in successful_from_logs
                else "destination exists"
            )
            manifest_writer.record_skipped(
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
            f"Attempting IDrive download for remote file: {relative_path}"
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
                staging_dir=staging_dir,
                destination_dir=folder_task.destination,
                replace_existing=overwrite_mode == "replace",
                cooldown_ms=cooldown_ms,
            )
            log_download_message(
                f"Moved download to destination: {downloaded_file.final_path}"
            )
        except BrowserClosedError:
            # The page is gone: this file is blameless, and every remaining file
            # would "fail" the same way. Propagate so the caller can recover on a
            # fresh page (re-queuing this folder) or abort cleanly.
            raise
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
            manifest_writer.record_failed(
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
        manifest_writer.record_downloaded(downloaded_file)


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
    staging_dir = staging_dir_for_destination(destination)
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
    started_at = datetime.now()
    run_started_monotonic = time.monotonic()
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

    manifest_path = build_manifest_path(downloads_dir, started_at)
    repo_root = downloads_dir.parents[2]
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

    manifest_writer = StreamingManifestWriter(
        manifest_path=manifest_path,
        repo_root=repo_root,
        url=url,
        destination=destination,
        started_at=started_at,
        progress_log_path=progress_log_path,
    )
    folders_processed = 0
    folders_since_recycle = 0
    folders_unavailable = 0
    recoveries_used = 0
    last_resource_sample_at = 0.0
    cmdline_markers = browser_cmdline_markers(profile_dir, browser_debug_url)
    home_url = idrive_home_url(url) if is_idrive_url(url) else None
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

                if (
                    home_url is not None
                    and folders_since_recycle >= PAGE_RECYCLE_FOLDER_INTERVAL
                ):
                    _recycle_page_renderer(
                        page, home_url=home_url, progress_logger=progress_logger
                    )
                    folders_since_recycle = 0

                visited_destinations.add(folder_task.destination)
                folders_processed += 1
                folders_since_recycle += 1
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
                if (
                    time.monotonic() - last_resource_sample_at
                    >= RESOURCE_SAMPLE_MIN_INTERVAL_SECONDS
                ):
                    _log_resource_sample(progress_logger, cmdline_markers)
                    last_resource_sample_at = time.monotonic()

                try:
                    _process_folder_task(
                        page=page,
                        folder_task=folder_task,
                        folder_queue=folder_queue,
                        base_destination=destination,
                        staging_dir=staging_dir,
                        downloads_dir=downloads_dir,
                        overwrite_mode=overwrite_mode,
                        successful_from_logs=successful_from_logs,
                        manifest_writer=manifest_writer,
                        progress_logger=progress_logger,
                        timeout_ms=timeout_ms,
                        cooldown_ms=cooldown_ms,
                        headless=headless,
                        use_folder_cache=use_folder_cache,
                    )
                except FolderUnavailableError as error:
                    # IDrive would not open this folder even after a few quick
                    # retries. Skip it (and its whole subtree) and keep the run
                    # going instead of failing the run or looping for a long time.
                    folders_unavailable += 1
                    log_download_message(
                        "Skipping folder IDrive would not open: "
                        f"{folder_task.url} ({error})"
                    )
                    progress_logger.log(
                        "folder_unavailable",
                        folderUrl=folder_task.url,
                        destination=str(folder_task.destination),
                        reason=str(error),
                    )
                    continue
                except BrowserClosedError as error:
                    recoveries_used += 1
                    # Every tab death is evidence: capture diagnostics whether or
                    # not recovery succeeds.
                    _capture_browser_crash_diagnostics(
                        engine=engine,
                        downloads_dir=downloads_dir,
                        run_started_monotonic=run_started_monotonic,
                        folders_processed=folders_processed,
                        current_folder=folder_task,
                        manifest_writer=manifest_writer,
                        progress_logger=progress_logger,
                        error=error,
                        cmdline_markers=cmdline_markers,
                        death_number=recoveries_used,
                    )
                    if recoveries_used > TAB_DEATH_RECOVERY_LIMIT:
                        log_download_message(
                            "Tab death recovery limit reached "
                            f"({TAB_DEATH_RECOVERY_LIMIT}); aborting the run"
                        )
                        raise
                    recovered_page = _reopen_page_after_tab_death(engine, page)
                    if recovered_page is None:
                        # The browser itself is gone; nothing to recover onto.
                        raise
                    page = recovered_page
                    # Retry the interrupted folder: it was blameless, and under
                    # overwrite=skip any files it already downloaded are skipped on
                    # the second pass (their discovered records repeat in the
                    # journal — acceptable).
                    visited_destinations.discard(folder_task.destination)
                    folder_queue.insert(0, folder_task)
                    folders_since_recycle = 0
                    progress_logger.log(
                        "tab_death_recovered",
                        recoveryNumber=recoveries_used,
                        recoveryLimit=TAB_DEATH_RECOVERY_LIMIT,
                        folderUrl=folder_task.url,
                    )
                    log_download_message(
                        f"Recovered from tab death {recoveries_used}/"
                        f"{TAB_DEATH_RECOVERY_LIMIT}; retrying folder: {folder_task.url}"
                    )

    except Exception as error:
        # A BrowserClosedError arriving here already wrote its crash-diagnostics
        # report in the folder loop (every tab death does, recovered or not).
        progress_logger.log("run_failed", reason=str(error))
        # Leave the partial journal on disk; do not publish a final manifest.
        manifest_writer.close()
        raise

    finished_at = datetime.now()
    counts = manifest_writer.finalize(finished_at)
    if folders_unavailable:
        log_download_message(
            f"{folders_unavailable} folder(s) skipped because IDrive would not "
            "open them (see folder_unavailable events in the progress log)"
        )
    report = DownloadFolderReport(
        url=url,
        destination=destination,
        started_at=started_at,
        finished_at=finished_at,
        counts=counts,
        manifest_path=manifest_path,
        progress_log_path=progress_log_path,
        folders_unavailable=folders_unavailable,
    )
    progress_logger.log(
        "run_finished",
        downloadedCount=counts.downloaded,
        skippedCount=counts.skipped,
        failedCount=counts.failed,
        foldersUnavailable=folders_unavailable,
        manifestPath=str(manifest_path),
        exitCode=report.exit_code,
    )
    return report
