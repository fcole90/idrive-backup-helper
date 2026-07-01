from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal


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
class ManifestHeader:
    """Run-level metadata read from a manifest without materializing its records."""

    manifest_path: Path
    url: str
    destination: Path


@dataclass(frozen=True)
class ManifestVerification:
    manifest_path: Path
    expected_files: int
    present_files: int
    missing_files: int

    @property
    def exit_code(self) -> int:
        return 1 if self.missing_files else 0


@dataclass(frozen=True)
class DownloadCounts:
    """Per-run tallies kept in memory in place of the O(files) record lists."""

    discovered: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0


@dataclass(frozen=True)
class DownloadFolderReport:
    url: str
    destination: Path
    started_at: datetime
    finished_at: datetime
    counts: DownloadCounts
    manifest_path: Path
    progress_log_path: Path | None = None

    @property
    def exit_code(self) -> int:
        return 1 if self.counts.failed else 0


@dataclass(frozen=True)
class FolderTask:
    url: str
    destination: Path
    expected_folder_name: str | None


type OverwriteMode = Literal["skip", "replace", "fail"]
