from datetime import datetime
import json
from pathlib import Path

from idrive_backup_helper.runlog.console import log, mirror_progress


def _iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def build_progress_log_path(
    downloads_dir: Path,
    started_at: datetime,
    *,
    prefix: str,
) -> Path:
    timestamp = started_at.strftime("%Y-%m-%dT%H-%M-%S")
    return downloads_dir / f"{prefix}-{timestamp}.ndjson"


class ProgressEventLogger:
    def __init__(self, progress_log_path: Path) -> None:
        self.progress_log_path = progress_log_path
        self._sequence = 0
        self._enabled = True
        self.progress_log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event_type: str, **fields: object) -> None:
        payload: dict[str, object] = {
            "timestamp": _iso_now(),
            "sequence": self._sequence,
            "event": event_type,
            **fields,
        }
        self._sequence += 1
        # The anonymized copy (when one is active) gets every event, even after
        # the raw file stops accepting writes.
        mirror_progress(payload)
        if not self._enabled:
            return

        try:
            with self.progress_log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload) + "\n")
        except OSError as error:
            log(
                "download-folder",
                "Progress log disabled after write error: {error}",
                error=error,
            )
            self._enabled = False
