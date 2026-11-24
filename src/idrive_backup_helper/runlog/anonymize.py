"""Turn private names, paths, URLs, and error text into stable tokens.

A token is a keyed hash of one value, so the same folder gets the same token in
every event and every run that shares the key, while the key (kept in the
protected secrets folder) stops anyone from guessing names by hashing common
ones. Paths and URLs are tokenized one segment at a time: files in the same
folder share that folder's token, and a remote folder and its local copy match.
"""

import hashlib
import hmac
import re
import secrets
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

TOKEN_HEX_LENGTH = 8
MIN_LITERAL_LENGTH = 3
MAX_TEXT_LENGTH = 800

# Segments that describe this tool or IDrive's routes, never the user's data.
SAFE_PATH_SEGMENTS = frozenset(
    {
        ".agents",
        "local",
        "secrets",
        "idrive",
        "runs",
        "playground",
        "anon-logs",
        "folder-cache",
        "browser-state",
        "idrive-chromium",
        ".idrive-staging",
    }
)
SAFE_URL_SEGMENTS = frozenset(
    {
        "idrive",
        "home",
        "login",
        "loginform",
        "step2",
        "evs",
        "v1",
        "downloadfile",
    }
)
SAFE_URL_HOST_SUFFIXES = ("idrive.com",)
SAFE_URL_HOSTS = frozenset({"localhost", "127.0.0.1"})
# Run files this tool names itself: they carry only a timestamp.
_RUN_FILE_RE = re.compile(
    r"^(download-folder|retry-manifest)-[a-z-]*\d{4}-\d{2}-\d{2}T[\d-]+" r"(\.[a-z]+)+$"
)
# A trailing extension is kept for debugging (a .dll may fail where a .jpg does
# not), but only when it looks like one: 1-4 letters or digits, one case, so
# "Mr.Smith" does not leak "Smith".
_EXTENSION_RE = re.compile(r"^(.+?)(\.(?:[a-z0-9]{1,4}|[A-Z0-9]{1,4}))$")
_NUMBER_RE = re.compile(r"^\d{1,6}$")
# Quoted text kept as is: website error codes such as "INVALID PATH", and the
# keys of IDrive's JSON error replies. Names the run already saw were replaced
# by the literal pass before quotes are looked at.
_ERROR_CODE_RE = re.compile(r"^[A-Z][A-Z0-9 _-]{1,39}$")
SAFE_QUOTED = frozenset({"desc", "status", "message", "error", "code", "result"})

_URL_RE = re.compile(r"[a-z][a-z0-9+.-]*://[^\s'\"<>)\]]+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_WINDOWS_PATH_RE = re.compile(r"(?<![\w])[A-Za-z]:[\\/][^\s'\"<>|]*")
_UNC_PATH_RE = re.compile(r"\\\\[^\s'\"<>|]+")
# Any run of non-space characters holding a path separator between word
# characters: catches POSIX paths, relative paths, and the tails of paths that
# a space split off from their drive letter.
_SEPARATED_RE = re.compile(r"[^\s'\"<>|(),;]*\w[\\/][^\s'\"<>|(),;]*")
# A quoted string, allowing backslash-escaped quotes inside it, as Playwright
# prints selectors such as "text=\"Report.pdf\"".
_QUOTED_RE = re.compile(r"'((?:[^'\\\n]|\\.){1,300})'|\"((?:[^\"\\\n]|\\.){1,300})\"")
# "~" marks a token: unlike "#" it means nothing in a URL, so a tokenized URL
# parses the same way when it is scrubbed again.
_TOKEN_RE = re.compile(r"^[a-z]~[0-9a-f]{%d}" % TOKEN_HEX_LENGTH)


def load_or_create_key(key_path: Path) -> bytes:
    """Read the anonymizer key, creating it on first use."""
    if key_path.exists():
        return bytes.fromhex(key_path.read_text(encoding="ascii").strip())
    key = secrets.token_bytes(32)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text(key.hex(), encoding="ascii")
    return key


class Anonymizer:
    def __init__(
        self,
        key: bytes,
        *,
        private_paths: Iterable[str | Path] = (),
        private_names: Iterable[str] = (),
    ) -> None:
        self._key = key
        self._literals: dict[str, str] = {}
        self._literal_re: re.Pattern[str] | None = None
        for path in private_paths:
            self.path(path)
            self.path(str(path).replace("\\", "/"))
        for name in private_names:
            self.name(name)

    def _hash(self, value: str) -> str:
        digest = hmac.new(self._key, value.encode("utf-8"), hashlib.sha256)
        return digest.hexdigest()[:TOKEN_HEX_LENGTH]

    def _remember(self, raw: str, replacement: str) -> None:
        if len(raw) < MIN_LITERAL_LENGTH or raw == replacement:
            return
        if self._literals.get(raw) != replacement:
            self._literals[raw] = replacement
            self._literal_re = None

    def name(self, value: str, *, prefix: str = "n") -> str:
        stripped = value.strip()
        if not stripped:
            return value
        match = _EXTENSION_RE.match(stripped)
        if match is not None and prefix != "d":
            token = f"{prefix}~{self._hash(stripped)}{match.group(2).lower()}"
        else:
            token = f"{prefix}~{self._hash(stripped)}"
        self._remember(stripped, token)
        return token

    def _segment(self, segment: str, *, is_last: bool, safe: frozenset[str]) -> str:
        if segment in ("", ".", "..") or segment.lower() in safe:
            return segment
        if _TOKEN_RE.match(segment):
            return segment
        if is_last and _RUN_FILE_RE.match(segment):
            return segment
        decoded = unquote(segment)
        has_extension = _EXTENSION_RE.match(decoded) is not None
        prefix = "f" if is_last and has_extension else "d"
        return self.name(decoded, prefix=prefix)

    def path(self, value: str | Path) -> str:
        raw = str(value)
        if not raw:
            return raw
        if _URL_RE.fullmatch(raw):
            return self.url(raw)
        prefix = ""
        rest = raw
        if raw.startswith(("\\\\", "//")):
            prefix, rest = raw[:2], raw[2:]
        elif re.match(r"^[A-Za-z]:", raw):
            prefix, rest = raw[:2], raw[2:]
        parts = re.split(r"([\\/])", rest)
        segment_indexes = [i for i, part in enumerate(parts) if part not in ("/", "\\")]
        last_index = segment_indexes[-1] if segment_indexes else -1
        tokenized = [
            (
                part
                if part in ("/", "\\")
                else self._segment(
                    part, is_last=index == last_index, safe=SAFE_PATH_SEGMENTS
                )
            )
            for index, part in enumerate(parts)
        ]
        result = prefix + "".join(tokenized)
        self._remember(raw, result)
        return result

    def url(self, value: str) -> str:
        try:
            parts = urlsplit(value)
        except ValueError:
            return self.name(value, prefix="u")
        host = (parts.hostname or "").lower()
        if host in SAFE_URL_HOSTS or host.endswith(SAFE_URL_HOST_SUFFIXES):
            netloc = host if parts.port is None else f"{host}:{parts.port}"
        else:
            netloc = self.name(host, prefix="h") if host else ""
        path_segments = parts.path.split("/")
        last = len(path_segments) - 1
        path = "/".join(
            self._segment(segment, is_last=index == last, safe=SAFE_URL_SEGMENTS)
            for index, segment in enumerate(path_segments)
        )
        query = "&".join(
            f"{key}={self._query_value(query_value)}"
            for key, query_value in parse_qsl(parts.query, keep_blank_values=True)
        )
        fragment = self.path(parts.fragment) if parts.fragment else ""
        result = f"{parts.scheme}://{netloc}{path}"
        if query:
            result += f"?{query}"
        if fragment:
            result += f"#{fragment}"
        self._remember(value, result)
        return result

    def _query_value(self, value: str) -> str:
        if value == "" or _NUMBER_RE.match(value):
            return value
        if "/" in value or "\\" in value:
            return self.path(value)
        return self.name(value)

    def glob(self, pattern: str) -> str:
        """Tokenize the literal segments of an exclude glob, keep its wildcards."""
        return "/".join(
            segment if any(c in segment for c in "*?[") else self.name(segment)
            for segment in pattern.split("/")
        )

    def _literal_pattern(self) -> re.Pattern[str] | None:
        if not self._literals:
            return None
        if self._literal_re is None:
            alternatives = sorted(self._literals, key=len, reverse=True)
            self._literal_re = re.compile(
                "|".join(re.escape(literal) for literal in alternatives)
            )
        return self._literal_re

    def text(self, value: str) -> str:
        """Scrub free text such as an error message or website reply."""
        text = value[:MAX_TEXT_LENGTH]
        if len(value) > MAX_TEXT_LENGTH:
            text += " …[truncated]"
        literal_re = self._literal_pattern()
        if literal_re is not None:
            text = literal_re.sub(lambda m: self._literals[m.group(0)], text)
        text = _URL_RE.sub(lambda m: self.url(m.group(0)), text)
        text = _EMAIL_RE.sub(lambda m: self.name(m.group(0), prefix="e"), text)
        text = _UNC_PATH_RE.sub(lambda m: self.path(m.group(0)), text)
        text = _WINDOWS_PATH_RE.sub(lambda m: self.path(m.group(0)), text)
        text = _SEPARATED_RE.sub(lambda m: self._path_unless_url(m.group(0)), text)
        return _QUOTED_RE.sub(self._quoted, text)

    def _path_unless_url(self, value: str) -> str:
        # URLs were handled by the URL step; tokens inside a path are kept by
        # _segment, so a second pass over a tokenized path changes nothing.
        if "://" in value:
            return value
        return self.path(value)

    def _quoted(self, match: re.Match[str]) -> str:
        quote = "'" if match.group(1) is not None else '"'
        inner = match.group(1) if match.group(1) is not None else match.group(2)
        if (
            _NUMBER_RE.match(inner)
            or _TOKEN_RE.match(inner)
            or _ERROR_CODE_RE.match(inner)
            or inner in SAFE_QUOTED
        ):
            return match.group(0)
        return f"{quote}{self.name(inner, prefix='q')}{quote}"
