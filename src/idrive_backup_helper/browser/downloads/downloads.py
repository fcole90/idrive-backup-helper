from pathlib import Path

from idrive_backup_helper.browser.downloads.download_cache import (
    load_folder_entries_cache,
    load_resume_success_relative_paths,
    write_folder_entries_cache,
)
from idrive_backup_helper.browser.downloads.download_entries import (
    ensure_raw_file_list,
    parse_remote_entries,
    parse_remote_files,
)
from idrive_backup_helper.browser.downloads.download_manifest import (
    StreamingManifestWriter,
    build_manifest_path,
    build_retry_manifest_path,
    ensure_destination_dir,
    iter_manifest_discovered_files,
    read_manifest_header,
    verify_download_manifest,
)
from idrive_backup_helper.browser.downloads.download_models import (
    DownloadCounts,
    DownloadedFile,
    DownloadFolderReport,
    FailedFile,
    FolderTask,
    ManifestFileRecord,
    ManifestHeader,
    ManifestVerification,
    OverwriteMode,
    RemoteEntries,
    RemoteFile,
    RemoteFolder,
    SkippedFile,
)
from idrive_backup_helper.browser.downloads.download_page import (
    load_folder_entries_with_retry,
)
from idrive_backup_helper.browser.downloads.download_progress import (
    ProgressEventLogger,
    build_progress_log_path,
)
from idrive_backup_helper.browser.downloads.download_run import download_current_folder
from idrive_backup_helper.browser.engine import BrowserConfig, BrowserEngine
from idrive_backup_helper.browser.retry_run import retry_missing_files_from_manifest

# Re-export stable surface so callers/tests don't need to import split modules.
__all__ = [
    "DownloadCounts",
    "DownloadFolderReport",
    "DownloadedFile",
    "FailedFile",
    "FolderTask",
    "ManifestFileRecord",
    "ManifestHeader",
    "ManifestVerification",
    "OverwriteMode",
    "ProgressEventLogger",
    "RemoteEntries",
    "RemoteFile",
    "RemoteFolder",
    "SkippedFile",
    "StreamingManifestWriter",
    "build_manifest_path",
    "build_progress_log_path",
    "build_retry_manifest_path",
    "download_current_folder",
    "ensure_destination_dir",
    "ensure_raw_file_list",
    "iter_manifest_discovered_files",
    "list_current_folder_files",
    "load_folder_entries_cache",
    "load_resume_success_relative_paths",
    "parse_remote_entries",
    "parse_remote_files",
    "read_manifest_header",
    "retry_missing_files_from_manifest",
    "verify_download_manifest",
    "write_folder_entries_cache",
]


def list_current_folder_files(
    *,
    profile_dir: Path,
    downloads_dir: Path,
    url: str,
    headless: bool,
    timeout_ms: int,
    browser_debug_url: str | None = None,
) -> list[RemoteFile]:
    config = BrowserConfig(
        profile_dir=profile_dir,
        staging_dir=downloads_dir,
        headless=headless,
        timeout_ms=timeout_ms,
        browser_debug_url=browser_debug_url,
    )

    with BrowserEngine(config) as engine:
        page = engine.current_page_or_new_page()
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
