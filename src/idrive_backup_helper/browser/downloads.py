from pathlib import Path

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
    verify_download_manifest,
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
from idrive_backup_helper.browser.download_page import load_folder_entries_with_retry
from idrive_backup_helper.browser.download_progress import (
    ProgressEventLogger,
    build_progress_log_path,
)
from idrive_backup_helper.browser.download_run import download_current_folder
from idrive_backup_helper.browser.engine import BrowserConfig, BrowserEngine
from idrive_backup_helper.browser.retry_run import retry_missing_files_from_manifest

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
