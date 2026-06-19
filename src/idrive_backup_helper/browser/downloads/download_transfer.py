from pathlib import Path

from playwright.sync_api import Page

from idrive_backup_helper.browser.downloads.download_models import (
    DownloadedFile,
    RemoteFile,
)
from idrive_backup_helper.browser.downloads.download_page import download_one_file
from idrive_backup_helper.filesystem.moves import move_download_to_destination


def transfer_remote_file_to_destination(
    *,
    page: Page,
    remote_file: RemoteFile,
    downloads_dir: Path,
    destination_dir: Path,
    replace_existing: bool,
    cooldown_ms: int,
) -> DownloadedFile:
    staged_path = download_one_file(
        page,
        remote_file,
        downloads_dir,
        cooldown_ms,
    )
    moved_path = move_download_to_destination(
        staged_path,
        destination_dir,
        remote_file.file_name,
        replace_existing=replace_existing,
    )
    return DownloadedFile(
        file_name=remote_file.file_name,
        staged_path=staged_path,
        final_path=moved_path,
    )
