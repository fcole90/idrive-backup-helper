from idrive_backup_helper.runlog.anonymize import Anonymizer
from idrive_backup_helper.runlog.fields import anonymize_fields, field_kind

KEY = bytes(range(32))


def test_field_kind_reads_camel_and_snake_case() -> None:
    assert field_kind("folderUrl") == "url"
    assert field_kind("finalPath") == "path"
    assert field_kind("relativePath") == "path"
    assert field_kind("destination") == "path"
    assert field_kind("fileName") == "name"
    assert field_kind("reason") == "text"
    assert field_kind("recovery_error") == "text"
    assert field_kind("excludePatterns") == "glob"
    assert field_kind("event") == "safe"
    assert field_kind("somethingNew") == "unknown"


def test_unknown_text_fields_never_pass_raw() -> None:
    result = anonymize_fields(Anonymizer(KEY), {"somethingNew": "Secret Folder"})

    assert result["somethingNew"] != "Secret Folder"
    assert str(result["somethingNew"]).startswith("x~")


def test_numbers_and_booleans_pass_whatever_the_field() -> None:
    fields: dict[str, object] = {
        "somethingNew": 3,
        "hung": True,
        "browserRssMb": 812.5,
        "detail": None,
    }

    assert anonymize_fields(Anonymizer(KEY), fields) == fields


def test_progress_event_is_anonymized_field_by_field() -> None:
    event: dict[str, object] = {
        "timestamp": "2026-11-24T10:00:00",
        "sequence": 4,
        "event": "file_downloaded",
        "fileName": "Tax Return.pdf",
        "finalPath": r"D:\Backup\Taxes\Tax Return.pdf",
        "folderUrl": "https://www.idrive.com/idrive/home/PC/D/Taxes",
        "excludePatterns": ["*.tmp", "Private/**"],
    }

    result = anonymize_fields(Anonymizer(KEY), event)

    assert result["event"] == "file_downloaded"
    assert result["sequence"] == 4
    assert result["excludePatterns"][0] == "*.tmp"  # type: ignore[index]
    flattened = repr(result)
    for private in ("Tax", "Backup", "Taxes", "Private", "PC"):
        assert private not in flattened
