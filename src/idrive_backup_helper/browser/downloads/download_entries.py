from typing import cast

from idrive_backup_helper.browser.downloads.download_models import (
    RemoteEntries,
    RemoteFile,
    RemoteFolder,
)


def ensure_raw_file_list(raw_files: object) -> list[object]:
    if not isinstance(raw_files, list):
        raise ValueError("Browser file list must be a JSON array.")

    return cast(list[object], raw_files)


def parse_remote_entries(raw_entries: list[object]) -> RemoteEntries:
    files: list[RemoteFile] = []
    folders: list[RemoteFolder] = []

    for index, item_object in enumerate(raw_entries):
        if not isinstance(item_object, dict):
            raise ValueError(f"Invalid file item at index {index}: expected object.")

        candidate_dict = cast(dict[object, object], item_object)
        normalized_item: dict[str, object] = {}
        for key_object, value_object in candidate_dict.items():
            if isinstance(key_object, str):
                normalized_item[key_object] = value_object

        entry_type = normalized_item.get("entryType")
        if entry_type == "file":
            file_name = normalized_item.get("fileName")
            row_index = normalized_item.get("rowIndex")
            server_size_text = normalized_item.get("serverSizeText")
            server_modified_text = normalized_item.get("serverModifiedText")

            if not isinstance(file_name, str) or not file_name.strip():
                raise ValueError(f"Invalid fileName at index {index}.")
            if not isinstance(row_index, int):
                raise ValueError(f"Invalid rowIndex at index {index}.")
            if server_size_text is not None and not isinstance(server_size_text, str):
                raise ValueError(f"Invalid serverSizeText at index {index}.")
            if server_modified_text is not None and not isinstance(
                server_modified_text, str
            ):
                raise ValueError(f"Invalid serverModifiedText at index {index}.")

            files.append(
                RemoteFile(
                    file_name=file_name,
                    row_index=row_index,
                    server_size_text=server_size_text,
                    server_modified_text=server_modified_text,
                )
            )
            continue

        if entry_type == "folder":
            folder_name = normalized_item.get("folderName")
            href = normalized_item.get("href")

            if not isinstance(folder_name, str) or not folder_name.strip():
                raise ValueError(f"Invalid folderName at index {index}.")
            if not isinstance(href, str) or not href.strip():
                raise ValueError(f"Invalid href at index {index}.")

            folders.append(RemoteFolder(folder_name=folder_name, href=href))
            continue

        raise ValueError(f"Invalid entryType at index {index}.")

    return RemoteEntries(files=files, folders=folders)


def parse_remote_files(raw_files: list[object]) -> list[RemoteFile]:
    return parse_remote_entries(raw_files).files
