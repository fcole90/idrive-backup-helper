import idrive_backup_helper.main as main_module
from pytest import MonkeyPatch


def test_main_delegates_to_cli(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(main_module, "cli_main", lambda: 7)
    assert main_module.main() == 7
