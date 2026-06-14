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
