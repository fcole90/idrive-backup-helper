"""The log sink used with --with-anon-log.

It keeps two logs. The raw console lines (and any traceback) go to a file in
the protected secrets folder, next to the raw progress log. The anonymized log,
which agents may read, gets every console line and progress event with private
values replaced by tokens, and the terminal shows the anonymized lines too, so
nothing private reaches a terminal an agent may be watching.
"""

import getpass
import json
import re
import traceback
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from idrive_backup_helper.filesystem.paths import anon_key_path, runs_dir
from idrive_backup_helper.runlog.anonymize import Anonymizer, load_or_create_key
from idrive_backup_helper.runlog.console import ConsoleLine, print_line
from idrive_backup_helper.runlog.fields import anonymize_fields

_EVENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class AnonRunLog:
    def __init__(
        self,
        *,
        anonymizer: Anonymizer,
        run_id: str,
        anon_log_path: Path,
        raw_console_path: Path,
    ) -> None:
        self.anonymizer = anonymizer
        self.run_id = run_id
        self.anon_log_path = anon_log_path
        self.raw_console_path = raw_console_path
        self._anon_enabled = True
        anon_log_path.parent.mkdir(parents=True, exist_ok=True)
        raw_console_path.parent.mkdir(parents=True, exist_ok=True)

    def console(self, line: ConsoleLine) -> None:
        self._append_raw(line.prefixed(line.render()))
        try:
            fields = anonymize_fields(self.anonymizer, line.fields)
            message = line.render(fields)
        except Exception:
            print_line(line, "[redaction failed; see the raw console log]")
            self._write_anon(
                {"event": "redaction_error", "source": line.source},
            )
            return
        print_line(line, message)
        self._write_anon(
            {
                "event": "console",
                "source": line.source,
                "stream": line.stream,
                "template": line.template,
                "message": message,
                "fields": fields,
            }
        )

    def progress(self, payload: Mapping[str, object]) -> None:
        try:
            record = anonymize_fields(self.anonymizer, payload)
        except Exception:
            event = payload.get("event")
            record = {
                "event": "redaction_error",
                "original_event": (
                    event
                    if isinstance(event, str) and _EVENT_NAME_RE.match(event)
                    else None
                ),
            }
        self._write_anon({"stream": "progress", **record})

    def record_exception(self, error: BaseException) -> None:
        """Keep the full traceback in the raw log only."""
        self._append_raw(
            "".join(traceback.format_exception(type(error), error, error.__traceback__))
        )

    def _append_raw(self, text: str) -> None:
        try:
            with self.raw_console_path.open("a", encoding="utf-8") as handle:
                handle.write(f"{_now()} {text}\n")
        except OSError:
            pass

    def _write_anon(self, record: Mapping[str, object]) -> None:
        if not self._anon_enabled:
            return
        line = {"timestamp": _now(), "run_id": self.run_id, **record}
        try:
            with self.anon_log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(line) + "\n")
        except OSError as error:
            self._anon_enabled = False
            print_line(
                ConsoleLine(source="anon-log", template=""),
                f"Anonymized log disabled after a write error ({type(error).__name__})",
            )


def open_anon_run_log(
    *,
    repo_root: Path,
    command: str,
    anon_dir: Path,
    started_at: datetime | None = None,
    private_values: Mapping[str, object] | None = None,
) -> AnonRunLog:
    """Create the sink for one CLI run.

    `private_values` are the run's private arguments (URL, destination, ...),
    keyed like log fields; tokenizing them up front lets free text that
    mentions them be scrubbed before they first appear in a field.
    """
    stamp = (started_at or datetime.now()).strftime("%Y-%m-%dT%H-%M-%S")
    run_id = f"{command}-{stamp}"
    anonymizer = Anonymizer(
        load_or_create_key(anon_key_path(repo_root)),
        private_paths=[Path.home(), repo_root],
        private_names=[getpass.getuser()],
    )
    if private_values:
        anonymize_fields(anonymizer, private_values)
    return AnonRunLog(
        anonymizer=anonymizer,
        run_id=run_id,
        anon_log_path=anon_dir / f"{run_id}.anon.ndjson",
        raw_console_path=runs_dir(repo_root) / f"{command}-console-{stamp}.log",
    )
