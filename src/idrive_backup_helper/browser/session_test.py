from idrive_backup_helper.browser.session import requires_login


def test_requires_login_detects_login_form_url() -> None:
    assert requires_login("https://www.idrive.com/idrive/login/loginForm") is True


def test_requires_login_detects_login_path_case_insensitively() -> None:
    assert requires_login("https://www.idrive.com/IDrive/Login/step2") is True


def test_requires_login_ignores_home_url() -> None:
    assert requires_login("https://www.idrive.com/idrive/home") is False
