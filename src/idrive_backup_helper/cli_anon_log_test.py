"""End-to-end leak test for --with-anon-log.

Drives a real download-folder run (browser faked) whose folders, files, URLs,
and errors carry made-up private names, then checks that none of them reach
the anonymized log or the terminal, while the raw logs in the secrets folder
keep them.
"""

import json
from pathlib import Path

import pytest

from idrive_backup_helper.browser.downloads import download_run
from idrive_backup_helper.browser.downloads.download_models import (
    DownloadedFile,
    RemoteEntries,
    RemoteFile,
    RemoteFolder,
)
from idrive_backup_helper.browser.downloads.download_run_test import (
    FakeBrowserEngine,
)
from idrive_backup_helper.cli import main
from idrive_backup_helper.filesystem.paths import default_anon_log_dir, runs_dir

ROOT_URL = "https://www.idrive.com/idrive/home/JANES-LAPTOP/D/Private%20Docs"
CHILD_URL = f"{ROOT_URL}/Family%20Holiday"
PRIVATE_STRINGS = (
    "JANES-LAPTOP",
    "Private Docs",
    "Private%20Docs",
    "Tax Return",
    "Medical Records",
    "Family Holiday",
    "Family%20Holiday",
    "beach-with-Jane",
    "Janes Backup",
)


def _file(name: str, row: int) -> RemoteFile:
    return RemoteFile(
        file_name=name, row_index=row, server_size_text=None, server_modified_text=None
    )


def _fake_entries(page: object, *, target_url: str, **_: object) -> RemoteEntries:
    if target_url.rstrip("/") == ROOT_URL:
        return RemoteEntries(
            files=[_file("Tax Return 2025.pdf", 1), _file("Medical Records.docx", 2)],
            folders=[RemoteFolder(folder_name="Family Holiday", href=CHILD_URL)],
        )
    return RemoteEntries(files=[_file("beach-with-Jane.jpg", 1)], folders=[])


def _fake_transfer(
    *, remote_file: RemoteFile, staging_dir: Path, destination_dir: Path, **_: object
) -> DownloadedFile:
    if remote_file.file_name == "Medical Records.docx":
        raise RuntimeError(
            "Timeout 30000ms exceeded.\nCall log:\n"
            f'  - waiting for locator("text=\\"{remote_file.file_name}\\"")\n'
            f"  - page at {ROOT_URL}\n"
            '  - server replied {"desc":"INVALID PATH"}'
        )
    destination_dir.mkdir(parents=True, exist_ok=True)
    final_path = destination_dir / remote_file.file_name
    final_path.write_text("x", encoding="utf-8")
    return DownloadedFile(
        file_name=remote_file.file_name,
        staged_path=staging_dir / remote_file.file_name,
        final_path=final_path,
    )


@pytest.fixture
def fake_repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    (repo_root / "pyproject.toml").write_text("", encoding="utf-8")
    monkeypatch.chdir(repo_root)
    monkeypatch.setattr(download_run, "BrowserEngine", FakeBrowserEngine)
    monkeypatch.setattr(download_run, "load_folder_entries_with_retry", _fake_entries)
    monkeypatch.setattr(
        download_run, "ensure_folder_loaded_for_download", lambda *a, **k: None
    )
    monkeypatch.setattr(
        download_run, "transfer_remote_file_to_destination", _fake_transfer
    )
    return repo_root


def _run(repo_root: Path, *extra: str) -> int:
    destination = repo_root.parent / "Janes Backup"
    return main(
        [
            "download-folder",
            "--url",
            ROOT_URL,
            "--to",
            str(destination),
            "--browser-debug-url",
            "http://127.0.0.1:9222",
            "--no-resume-logs",
            *extra,
        ]
    )


def _assert_no_private(text: str) -> None:
    for private in PRIVATE_STRINGS:
        assert private.lower() not in text.lower(), private


def test_anon_log_and_terminal_hold_no_private_names(
    fake_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = _run(fake_repo, "--with-anon-log")

    captured = capsys.readouterr()
    anon_logs = list(default_anon_log_dir(fake_repo).glob("*.anon.ndjson"))
    assert exit_code != 0  # one file failed
    assert len(anon_logs) == 1
    anon_text = anon_logs[0].read_text(encoding="utf-8")
    _assert_no_private(anon_text)
    _assert_no_private(captured.out + captured.err)

    events = [json.loads(line) for line in anon_text.splitlines()]
    names = {event["event"] for event in events}
    assert {"console", "run_started", "file_downloaded", "file_failed"} <= names
    failed = next(event for event in events if event["event"] == "file_failed")
    assert "INVALID PATH" in failed["reason"]
    assert "Timeout 30000ms exceeded." in failed["reason"]
    assert "Anonymized log: .agents" in captured.out

    raw_console = next(runs_dir(fake_repo).glob("download-folder-console-*.log"))
    assert "Medical Records" in raw_console.read_text(encoding="utf-8")


def test_without_the_flag_the_terminal_stays_raw(
    fake_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(fake_repo)

    assert "Family Holiday" in capsys.readouterr().out
    assert not default_anon_log_dir(fake_repo).exists()


def test_custom_anon_dir_is_used(fake_repo: Path, tmp_path: Path) -> None:
    anon_dir = tmp_path / "monitor-logs"

    _run(fake_repo, f"--with-anon-log={anon_dir}")

    assert len(list(anon_dir.glob("*.anon.ndjson"))) == 1


def test_unexpected_errors_keep_their_traceback_out_of_the_terminal(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def crash(*_: object, **__: object) -> RemoteEntries:
        raise ValueError(f"cannot parse listing at {ROOT_URL}")

    monkeypatch.setattr(download_run, "load_folder_entries_with_retry", crash)

    exit_code = _run(fake_repo, "--with-anon-log")

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Unexpected ValueError" in captured.err
    _assert_no_private(captured.out + captured.err)
    raw_console = next(runs_dir(fake_repo).glob("download-folder-console-*.log"))
    raw_text = raw_console.read_text(encoding="utf-8")
    assert "Traceback" in raw_text
    assert "JANES-LAPTOP" in raw_text
