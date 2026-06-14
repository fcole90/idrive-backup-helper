from dataclasses import dataclass
from pathlib import Path
from typing import cast

from playwright.sync_api import Page

from idrive_backup_helper.browser.engine import BrowserConfig, BrowserEngine


@dataclass(frozen=True)
class RemoteFile:
    file_name: str
    row_index: int
    server_size_text: str | None
    server_modified_text: str | None


def _js_asset_path(name: str) -> Path:
    return Path(__file__).resolve().parent / "js" / name


def _load_js_asset(name: str) -> str:
    asset_path = _js_asset_path(name)
    if not asset_path.exists():
        raise RuntimeError(f"Missing browser script asset: {asset_path}")

    return asset_path.read_text(encoding="utf-8")


def ensure_raw_file_list(raw_files: object) -> list[object]:
    if not isinstance(raw_files, list):
        raise ValueError("Browser file list must be a JSON array.")

    return cast(list[object], raw_files)


def parse_remote_files(raw_files: list[object]) -> list[RemoteFile]:

    parsed: list[RemoteFile] = []
    for index, item_object in enumerate(raw_files):
        if not isinstance(item_object, dict):
            raise ValueError(f"Invalid file item at index {index}: expected object.")

        candidate_dict = cast(dict[object, object], item_object)
        normalized_item: dict[str, object] = {}
        for key_object, value_object in candidate_dict.items():
            if isinstance(key_object, str):
                normalized_item[key_object] = value_object

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

        parsed.append(
            RemoteFile(
                file_name=file_name,
                row_index=row_index,
                server_size_text=server_size_text,
                server_modified_text=server_modified_text,
            )
        )

    return parsed


def _evaluate_current_folder_files(page: Page) -> list[RemoteFile]:
    script = _load_js_asset("list_current_folder_files.js")
    raw_files: object = page.evaluate(
        script,
        {
            "scrollIntervalMs": 350,
            "maxIdleTicks": 3,
        },
    )
    return parse_remote_files(ensure_raw_file_list(raw_files))


def list_current_folder_files(
    *,
    profile_dir: Path,
    downloads_dir: Path,
    url: str,
    headless: bool,
    timeout_ms: int,
) -> list[RemoteFile]:
    config = BrowserConfig(
        profile_dir=profile_dir,
        downloads_dir=downloads_dir,
        headless=headless,
        timeout_ms=timeout_ms,
    )

    with BrowserEngine(config) as engine:
        page = engine.new_page()
        page.goto(url, wait_until="domcontentloaded")
        return _evaluate_current_folder_files(page)
