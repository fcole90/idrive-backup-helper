"""The one way this app writes to the console.

Callers pass a template and named fields, `log("cli", "Moved {file_name}",
file_name=name)`, never a pre-formatted f-string: the field names tell the
anonymizer which parts are private. Lines go to the active sink, which by
default prints them as before.
"""

import sys
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Literal, Protocol

type ConsoleStream = Literal["stdout", "stderr"]


@dataclass(frozen=True)
class ConsoleLine:
    source: str | None
    template: str
    fields: Mapping[str, object] = field(default_factory=dict[str, object])
    stream: ConsoleStream = "stdout"

    def render(self, values: Mapping[str, object] | None = None) -> str:
        try:
            return self.template.format_map(dict(values or self.fields))
        except KeyError, IndexError, ValueError:
            return self.template

    def prefixed(self, message: str) -> str:
        return message if self.source is None else f"[{self.source}] {message}"


class LogSink(Protocol):
    def console(self, line: ConsoleLine) -> None: ...

    def progress(self, payload: Mapping[str, object]) -> None: ...


def print_line(line: ConsoleLine, message: str) -> None:
    stream = sys.stderr if line.stream == "stderr" else sys.stdout
    print(line.prefixed(message), file=stream, flush=True)


class PlainSink:
    """Print raw lines to the terminal; progress events stay in their own file."""

    def console(self, line: ConsoleLine) -> None:
        print_line(line, line.render())

    def progress(self, payload: Mapping[str, object]) -> None:
        pass


_active_sink: LogSink = PlainSink()


def log(source: str | None, template: str, /, **fields: object) -> None:
    _active_sink.console(ConsoleLine(source=source, template=template, fields=fields))


def log_error(template: str, /, **fields: object) -> None:
    _active_sink.console(
        ConsoleLine(source=None, template=template, fields=fields, stream="stderr")
    )


def mirror_progress(payload: Mapping[str, object]) -> None:
    _active_sink.progress(payload)


@contextmanager
def use_sink(sink: LogSink) -> Generator[LogSink]:
    global _active_sink
    previous = _active_sink
    _active_sink = sink
    try:
        yield sink
    finally:
        _active_sink = previous
