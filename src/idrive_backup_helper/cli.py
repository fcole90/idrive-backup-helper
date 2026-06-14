import argparse
import sys
from collections.abc import Sequence

from idrive_backup_helper.browser.session import login_and_save_state
from idrive_backup_helper.filesystem.paths import browser_profile_dir, find_repo_root


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

    return parser


def _run_auth(url: str) -> None:
    repo_root = find_repo_root()
    profile_dir = browser_profile_dir(repo_root)
    login_and_save_state(profile_dir=profile_dir, start_url=url)

    print(f"Saved browser state under: {profile_dir}")
    print("Next step: uv run main download-folder --url <FOLDER_URL> --to <DEST_PATH>")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "auth":
            _run_auth(args.url)
            return 0
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 2

    parser.error(f"Unsupported command: {args.command}")
    return 2
