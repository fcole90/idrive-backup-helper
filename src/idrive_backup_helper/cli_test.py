from idrive_backup_helper.cli import build_parser


def test_build_parser_accepts_auth_command() -> None:
    parser = build_parser()
    args = parser.parse_args(["auth"])

    assert args.command == "auth"
    assert args.url == "https://www.idrive.com/idrive/login/loginForm"


def test_build_parser_accepts_auth_url_override() -> None:
    parser = build_parser()
    args = parser.parse_args(["auth", "--url", "https://example.com"])

    assert args.command == "auth"
    assert args.url == "https://example.com"


def test_build_parser_accepts_browse_command() -> None:
    parser = build_parser()
    args = parser.parse_args(["browse"])

    assert args.command == "browse"
    assert args.url == "https://www.idrive.com/idrive/home"


def test_build_parser_accepts_browse_url_override() -> None:
    parser = build_parser()
    args = parser.parse_args(["browse", "--url", "https://example.com/home"])

    assert args.command == "browse"
    assert args.url == "https://example.com/home"


def test_build_parser_accepts_download_folder_command() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "download-folder",
            "--url",
            "https://example.com/folder",
            "--to",
            "/tmp/output",
        ]
    )

    assert args.command == "download-folder"
    assert args.url == "https://example.com/folder"
    assert str(args.to) == "/tmp/output"
    assert args.headed is False
    assert args.timeout_ms == 120_000
    assert args.cooldown_ms == 1_500
    assert args.overwrite == "skip"
