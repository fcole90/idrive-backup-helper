import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from idrive_backup_helper.browser.downloads import list_current_folder_files
from idrive_backup_helper.browser.session import login_and_save_state
from idrive_backup_helper.filesystem.paths import (
    browser_profile_dir,
    downloads_dir,
    find_repo_root,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="IDrive backup helper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    auth_parser = subparsers.add_parser(
        "auth", help="Open a headed browser and cache IDrive login state"
    )
    auth_parser.add_argument(
        "--url",
        default="https://www.idrive.com/idrive/login/loginForm",
        help="IDrive URL to open for login",
    )

    download_parser = subparsers.add_parser(
        "download-folder",
        help="List visible files in one IDrive web folder (download step pending)",
    )
    download_parser.add_argument("--url", required=True, help="IDrive folder URL")
    download_parser.add_argument(
        "--to",
        required=True,
        type=Path,
        help="Local destination directory for downloaded files",
    )
    download_parser.add_argument(
        "--headed",
        action="store_true",
        help="Run browser in headed mode for debugging",
    )
    download_parser.add_argument(
        "--timeout-ms",
        type=int,
        default=120_000,
        help="Playwright timeout for page operations",
    )

    return parser


def _run_auth(url: str) -> None:
    repo_root = find_repo_root()
    profile_dir = browser_profile_dir(repo_root)
    login_and_save_state(profile_dir=profile_dir, start_url=url)

    print(f"Saved browser state under: {profile_dir}")
    print("Next step: uv run main download-folder --url <FOLDER_URL> --to <DEST_PATH>")


def _run_download_folder(
    *,
    url: str,
    destination: Path,
    headed: bool,
    timeout_ms: int,
) -> None:
    repo_root = find_repo_root()
    profile_dir = browser_profile_dir(repo_root)

    if not profile_dir.exists():
        raise RuntimeError("Missing browser auth state. Run: uv run main auth")

    staged_downloads_dir = downloads_dir(repo_root)
    files = list_current_folder_files(
        profile_dir=profile_dir,
        downloads_dir=staged_downloads_dir,
        url=url,
        headless=not headed,
        timeout_ms=timeout_ms,
    )

    print(f"Destination: {destination}")
    print(f"Found {len(files)} visible file(s) in current IDrive folder:")
    for remote_file in files:
        print(f"- {remote_file.file_name}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "auth":
            _run_auth(args.url)
            return 0
        if args.command == "download-folder":
            _run_download_folder(
                url=args.url,
                destination=args.to,
                headed=args.headed,
                timeout_ms=args.timeout_ms,
            )
            return 0
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 2

    parser.error(f"Unsupported command: {args.command}")
    return 2
