"""Safe connection-string parsing, redaction, and lineage normalization.

The public parser intentionally returns only values that can describe lineage. Authentication
material and values for unknown keys are replaced while parsing, rather than relying on every
later caller to remember to redact them.
"""

from __future__ import annotations

import ntpath
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum

REDACTED = "<redacted>"

# These names are retained for useful summaries, but their values are never exposed.
SECRET_KEYS = frozenset(
    {
        "access token",
        "account",
        "authentication",
        "client id",
        "client secret",
        "credential",
        "integrated security",
        "password",
        "persist security info",
        "pwd",
        "secret",
        "token",
        "trusted connection",
        "trusted_connection",
        "uid",
        "user",
        "user id",
        "user name",
        "userid",
        "username",
    }
)

# Only fields that can establish a static datasource identity may retain their values.
LINEAGE_KEYS = frozenset(
    {
        "addr",
        "address",
        "attach db filename",
        "attachdbfilename",
        "database",
        "data source",
        "dbq",
        "driver",
        "dsn",
        "file dsn",
        "file name",
        "filedsn",
        "filename",
        "host",
        "hostname",
        "initial catalog",
        "network address",
        "port",
        "provider",
        "schema",
        "server",
    }
)

_FILE_MARKER_PLATFORMS = {
    "dbase 5.0": "dbase",
    "dbase iii": "dbase",
    "dbase iv": "dbase",
    "excel 5.0": "excel",
    "excel 8.0": "excel",
    "excel 12.0": "excel",
    "excel 12.0 macro": "excel",
    "excel 12.0 xml": "excel",
    "html export": "html",
    "html import": "html",
    "ms access": "access",
    "text": "text",
}
_KNOWN_MARKERS = frozenset({"odbc", *_FILE_MARKER_PLATFORMS})
_KEY_WHITESPACE = re.compile(r"\s+")
_VALUE_WHITESPACE = re.compile(r"\s+")
_SQL_SERVER_DRIVER = re.compile(
    r"(?:microsoft\s+)?odbc\s+driver\s+(\d+)\s+for\s+sql\s+server",
    re.IGNORECASE,
)
_EMBEDDED_SECRET = re.compile(
    r"(?:^|[;?&\s])(?:password|pwd|token|secret|uid|user(?:[\s_]+(?:id|name))?|"
    r"userid|username|account|credential|client[\s_]+(?:id|secret)|"
    r"authentication|integrated[\s_]+security|persist[\s_]+security[\s_]+info|"
    r"trusted[\s_]+connection)\s*=",
    re.IGNORECASE,
)
_URI_USERINFO = re.compile(r"://[^/@\s]+@")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_WINDOWS_FILE_SUFFIX = re.compile(
    r"\.(?:accdb|mdb|accde|mde|xlsx?|xlsb|csv|txt|dsn|dbf|db|sqlite|mdf)$",
    re.IGNORECASE,
)


class ConnectionParseIssue(StrEnum):
    """Safe issue codes; none contain source connection-string text."""

    EMPTY_KEY = "empty_key"
    MISSING_EQUALS = "missing_equals"
    TRAILING_CHARACTERS = "trailing_characters"
    UNTERMINATED_BRACE = "unterminated_brace"
    UNTERMINATED_QUOTE = "unterminated_quote"


class ConnectionStringParseError(ValueError):
    """Raised by strict parsing without echoing the source value."""

    def __init__(self, issue: ConnectionParseIssue, offset: int) -> None:
        self.issue = issue
        self.offset = offset
        super().__init__(f"Malformed connection string ({issue.value} at offset {offset})")


@dataclass(frozen=True, slots=True)
class ConnectionParseDiagnostic:
    """A content-free parse issue plus its character offset."""

    issue: ConnectionParseIssue
    offset: int


@dataclass(frozen=True, slots=True)
class ConnectionParameter:
    """One sanitized parameter, retained in source order."""

    key: str
    value: str
    ordinal: int
    is_lineage: bool
    redacted: bool


@dataclass(frozen=True, slots=True)
class ParsedConnectionString:
    """A sanitized parse that retains duplicates and structural issue codes."""

    parameters: tuple[ConnectionParameter, ...]
    markers: tuple[str, ...]
    issues: tuple[ConnectionParseIssue, ...]
    diagnostics: tuple[ConnectionParseDiagnostic, ...]

    @property
    def malformed(self) -> bool:
        return bool(self.issues)

    @property
    def duplicates(self) -> tuple[str, ...]:
        counts = Counter(parameter.key for parameter in self.parameters)
        return tuple(sorted(key for key, count in counts.items() if count > 1))

    def values_for(self, key: str) -> tuple[str, ...]:
        normalized = _normalize_key(key)
        return tuple(
            parameter.value for parameter in self.parameters if parameter.key == normalized
        )

    def as_dict(self) -> dict[str, str]:
        """Return the legacy last-value-wins mapping, using only sanitized values."""
        return {parameter.key: parameter.value for parameter in self.parameters}


@dataclass(frozen=True, slots=True)
class NormalizedConnectionIdentity:
    """Stable, non-secret identity fields suitable for lineage comparison."""

    platform: str
    driver: str | None = None
    dsn: str | None = None
    server: str | None = None
    database: str | None = None
    file: str | None = None
    conflicts: tuple[str, ...] = ()

    @property
    def key(self) -> tuple[str, str, str, str, str, str]:
        return (
            self.platform,
            self.driver or "",
            self.dsn or "",
            self.server or "",
            self.database or "",
            self.file or "",
        )


def parse_connection_string_details(
    value: str, *, strict: bool = False
) -> ParsedConnectionString:
    """Parse a connection string with quote/brace-aware finite-state scanning.

    Values for credentials and unknown keys are redacted in the returned object. Duplicate
    parameters remain available in ``parameters`` while ``as_dict`` preserves the historical
    last-value-wins behavior.
    """
    segments, diagnostics = _split_segments(value)
    pending: list[tuple[str, str, int]] = []
    markers: list[str] = []

    for ordinal, (segment, segment_offset) in enumerate(segments):
        stripped = segment.strip()
        if not stripped:
            continue
        equals_at = stripped.find("=")
        if equals_at < 0:
            marker = _normalize_key(stripped)
            if marker in _KNOWN_MARKERS:
                markers.append(marker)
            else:
                diagnostics.append(
                    ConnectionParseDiagnostic(
                        ConnectionParseIssue.MISSING_EQUALS,
                        segment_offset,
                    )
                )
            continue
        raw_key = stripped[:equals_at]
        if not raw_key.strip():
            diagnostics.append(
                ConnectionParseDiagnostic(ConnectionParseIssue.EMPTY_KEY, segment_offset)
            )
            continue
        pending.append((_normalize_key(raw_key), stripped[equals_at + 1 :], ordinal))

    deduplicated_diagnostics = tuple(dict.fromkeys(diagnostics))
    deduplicated_issues = tuple(
        dict.fromkeys(diagnostic.issue for diagnostic in deduplicated_diagnostics)
    )
    if strict and deduplicated_diagnostics:
        first = deduplicated_diagnostics[0]
        raise ConnectionStringParseError(first.issue, first.offset)

    # A broken wrapper can turn a secret-looking suffix into part of an otherwise allowlisted
    # value. On any structural failure, withhold every value from the malformed source.
    force_redaction = any(
        issue
        in {
            ConnectionParseIssue.TRAILING_CHARACTERS,
            ConnectionParseIssue.UNTERMINATED_BRACE,
            ConnectionParseIssue.UNTERMINATED_QUOTE,
        }
        for issue in deduplicated_issues
    )
    parameters = tuple(
        _sanitize_parameter(key, raw_value, ordinal, force_redaction=force_redaction)
        for key, raw_value, ordinal in pending
    )
    return ParsedConnectionString(
        parameters=parameters,
        markers=tuple(dict.fromkeys(markers)),
        issues=deduplicated_issues,
        diagnostics=deduplicated_diagnostics,
    )


def parse_connection_string(value: str) -> dict[str, str]:
    """Return the compatible key/value mapping without exposing non-lineage values."""
    return parse_connection_string_details(value).as_dict()


def redact_connection_string(value: str) -> str:
    """Return a deterministic summary containing no credential or unknown-key values."""
    if not value:
        return ""
    parsed = parse_connection_string_details(value)
    parts = [*parsed.markers]
    parts.extend(
        f"{parameter.key}={_render_value(parameter.value)}" for parameter in parsed.parameters
    )
    if parsed.issues:
        parts.append("<malformed>")
    return ";".join(parts) if parts else REDACTED


def infer_platform(pairs: Mapping[str, str]) -> str:
    """Infer a canonical display platform from static, already-sanitized fields."""
    safe_pairs = {
        _normalize_key(str(key)): _safe_lineage_value(str(value))
        for key, value in pairs.items()
        if _normalize_key(str(key)) in LINEAGE_KEYS
    }
    platform_hints = " ".join(
        value for key, value in safe_pairs.items() if key in {"driver", "provider"}
    ).casefold()
    file_hints = " ".join(
        value
        for key, value in safe_pairs.items()
        if key
        in {
            "attach db filename",
            "attachdbfilename",
            "data source",
            "dbq",
            "file name",
            "filename",
        }
    ).casefold()
    if "oracle" in platform_hints or platform_hints.startswith("ora"):
        return "Oracle"
    if "postgres" in platform_hints or "psqlodbc" in platform_hints:
        return "PostgreSQL"
    if "mysql" in platform_hints or "mariadb" in platform_hints:
        return "MySQL/MariaDB"
    if "snowflake" in platform_hints:
        return "Snowflake"
    if (
        "sql server" in platform_hints
        or "sqloledb" in platform_hints
        or "msoledbsql" in platform_hints
        or "msodbcsql" in platform_hints
    ):
        return "SQL Server"
    if (
        ".accdb" in file_hints
        or ".mdb" in file_hints
        or "microsoft.ace.oledb" in platform_hints
        or "microsoft.jet.oledb" in platform_hints
        or "microsoft access driver" in platform_hints
    ):
        return "Access"
    return "Unknown"


def normalize_connection_identity(
    value: str | Mapping[str, str] | ParsedConnectionString,
) -> NormalizedConnectionIdentity:
    """Normalize the allowlisted identity in a connection string or registry mapping."""
    if isinstance(value, str):
        parsed = parse_connection_string_details(value)
        pairs = _group_parameter_values(parsed)
        markers = parsed.markers
        structural_conflicts = ("connection_string",) if parsed.malformed else ()
    elif isinstance(value, ParsedConnectionString):
        pairs = _group_parameter_values(value)
        markers = value.markers
        structural_conflicts = ("connection_string",) if value.malformed else ()
    else:
        pairs = _group_mapping_values(value)
        markers = ()
        structural_conflicts = ()

    driver, driver_conflict = _select_normalized(
        pairs, ("driver", "provider"), _normalize_driver
    )
    dsn, dsn_conflict = _select_normalized(pairs, ("dsn",), _normalize_identifier)
    file_aliases: tuple[str, ...] = (
        "dbq",
        "file name",
        "filename",
        "attachdbfilename",
        "attach db filename",
    )
    database_is_file = any(marker in _FILE_MARKER_PLATFORMS for marker in markers)
    if database_is_file:
        file_aliases = (*file_aliases, "database")
    file_value, file_conflict = _select_normalized(pairs, file_aliases, _normalize_file)

    data_source_values = pairs.get("data source", ())
    data_source_is_file = any(_looks_like_file(item) for item in data_source_values)
    if file_value is None and data_source_is_file:
        file_value, data_source_file_conflict = _select_values(
            data_source_values, _normalize_file
        )
        file_conflict = file_conflict or data_source_file_conflict

    server_aliases: tuple[str, ...] = (
        "server",
        "address",
        "addr",
        "network address",
        "host",
        "hostname",
    )
    if not data_source_is_file:
        server_aliases = (*server_aliases, "data source")
    server, server_conflict = _select_normalized(pairs, server_aliases, _normalize_server)
    port, port_conflict = _select_normalized(pairs, ("port",), _normalize_port)
    if server is not None and port not in {None, "1433"}:
        server = f"{server},{port}"
    server_conflict = server_conflict or port_conflict
    database_aliases = ("initial catalog",) if database_is_file else ("database", "initial catalog")
    database, database_conflict = _select_normalized(
        pairs, database_aliases, _normalize_identifier
    )

    flat_pairs = {key: values[-1] for key, values in pairs.items() if values}
    platform = {
        "Access": "access",
        "MySQL/MariaDB": "mysql_mariadb",
        "Oracle": "oracle",
        "PostgreSQL": "postgresql",
        "Snowflake": "snowflake",
        "SQL Server": "sql_server",
        "Unknown": "unknown",
    }[infer_platform(flat_pairs)]
    for marker in markers:
        marker_platform = _FILE_MARKER_PLATFORMS.get(marker)
        if marker_platform is not None and platform == "unknown":
            platform = marker_platform
            break

    conflicts = list(structural_conflicts)
    for field, conflict in (
        ("driver", driver_conflict),
        ("dsn", dsn_conflict),
        ("server", server_conflict),
        ("database", database_conflict),
        ("file", file_conflict),
    ):
        if conflict:
            conflicts.append(field)
    return NormalizedConnectionIdentity(
        platform=platform,
        driver=driver,
        dsn=dsn,
        server=server,
        database=database,
        file=file_value,
        conflicts=tuple(dict.fromkeys(conflicts)),
    )


def merge_connection_identities(
    base: NormalizedConnectionIdentity,
    override: NormalizedConnectionIdentity,
) -> NormalizedConnectionIdentity:
    """Overlay explicit connection-string fields on statically resolved defaults."""
    return NormalizedConnectionIdentity(
        platform=override.platform if override.platform != "unknown" else base.platform,
        driver=override.driver or base.driver,
        dsn=override.dsn or base.dsn,
        server=override.server or base.server,
        database=override.database or base.database,
        file=override.file or base.file,
        conflicts=tuple(dict.fromkeys((*base.conflicts, *override.conflicts))),
    )


def _split_segments(
    value: str,
) -> tuple[list[tuple[str, int]], list[ConnectionParseDiagnostic]]:
    segments: list[tuple[str, int]] = []
    diagnostics: list[ConnectionParseDiagnostic] = []
    buffer: list[str] = []
    segment_start = 0
    quote: str | None = None
    in_brace = False
    seen_equals = False
    value_started = False
    wrapper_closed = False
    index = 0

    def finish_segment(next_start: int) -> None:
        nonlocal buffer, seen_equals, segment_start, value_started, wrapper_closed
        segments.append(("".join(buffer), segment_start))
        buffer = []
        segment_start = next_start
        seen_equals = False
        value_started = False
        wrapper_closed = False

    while index < len(value):
        character = value[index]
        if quote is not None:
            buffer.append(character)
            if character == quote:
                if index + 1 < len(value) and value[index + 1] == quote:
                    buffer.append(value[index + 1])
                    index += 2
                    continue
                quote = None
                wrapper_closed = True
            index += 1
            continue

        if in_brace:
            buffer.append(character)
            if character == "}":
                if index + 1 < len(value) and value[index + 1] == "}":
                    buffer.append(value[index + 1])
                    index += 2
                    continue
                in_brace = False
                wrapper_closed = True
            index += 1
            continue

        if character == ";":
            finish_segment(index + 1)
            index += 1
            continue

        if not seen_equals and character == "=":
            seen_equals = True
            buffer.append(character)
            index += 1
            continue

        if seen_equals and not value_started:
            if character.isspace():
                buffer.append(character)
                index += 1
                continue
            value_started = True
            if character in {'"', "'"}:
                quote = character
            elif character == "{":
                in_brace = True
            buffer.append(character)
            index += 1
            continue

        if wrapper_closed and not character.isspace():
            diagnostics.append(
                ConnectionParseDiagnostic(ConnectionParseIssue.TRAILING_CHARACTERS, index)
            )
            wrapper_closed = False
        buffer.append(character)
        index += 1

    if quote is not None:
        diagnostics.append(
            ConnectionParseDiagnostic(ConnectionParseIssue.UNTERMINATED_QUOTE, len(value))
        )
    if in_brace:
        diagnostics.append(
            ConnectionParseDiagnostic(ConnectionParseIssue.UNTERMINATED_BRACE, len(value))
        )
    finish_segment(len(value))
    return segments, diagnostics


def _sanitize_parameter(
    key: str, raw_value: str, ordinal: int, *, force_redaction: bool
) -> ConnectionParameter:
    recognized_key = key if key in LINEAGE_KEYS or key in SECRET_KEYS else "unknown"
    is_lineage = key in LINEAGE_KEYS
    unwrapped = _unwrap_value(raw_value)
    safe_value = _safe_lineage_value(unwrapped) if is_lineage else REDACTED
    redacted = force_redaction or not is_lineage or safe_value == REDACTED
    if redacted:
        safe_value = REDACTED
    return ConnectionParameter(
        key=recognized_key,
        value=safe_value,
        ordinal=ordinal,
        is_lineage=is_lineage,
        redacted=redacted,
    )


def _unwrap_value(value: str) -> str:
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == "{" and stripped[-1] == "}":
        return stripped[1:-1].replace("}}", "}")
    if len(stripped) >= 2 and stripped[0] in {'"', "'"} and stripped[-1] == stripped[0]:
        quote = stripped[0]
        return stripped[1:-1].replace(quote * 2, quote)
    return stripped


def _safe_lineage_value(value: str) -> str:
    compact = _CONTROL_CHARACTERS.sub(" ", value).strip()
    if _EMBEDDED_SECRET.search(compact) or _URI_USERINFO.search(compact):
        return REDACTED
    if len(compact) > 2048:
        return REDACTED
    return compact


def _render_value(value: str) -> str:
    if value == REDACTED:
        return value
    if ";" in value or "}" in value or value != value.strip():
        return "{" + value.replace("}", "}}") + "}"
    return value


def _normalize_key(key: str) -> str:
    return _KEY_WHITESPACE.sub(" ", key.strip()).casefold()


def _group_parameter_values(parsed: ParsedConnectionString) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = {}
    for parameter in parsed.parameters:
        if parameter.is_lineage and not parameter.redacted:
            grouped.setdefault(parameter.key, []).append(parameter.value)
    return {key: tuple(values) for key, values in grouped.items()}


def _group_mapping_values(values: Mapping[str, str]) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = {}
    for raw_key, raw_value in values.items():
        key = _normalize_key(str(raw_key))
        if key not in LINEAGE_KEYS:
            continue
        safe_value = _safe_lineage_value(str(raw_value))
        if safe_value == REDACTED:
            continue
        grouped.setdefault(key, []).append(safe_value)
    return {key: tuple(items) for key, items in grouped.items()}


def _select_normalized(
    grouped: Mapping[str, tuple[str, ...]],
    aliases: tuple[str, ...],
    normalizer: Callable[[str], str],
) -> tuple[str | None, bool]:
    values = tuple(item for alias in aliases for item in grouped.get(alias, ()))
    return _select_values(values, normalizer)


def _select_values(
    values: tuple[str, ...], normalizer: Callable[[str], str]
) -> tuple[str | None, bool]:
    normalized = [normalizer(value) for value in values]
    unique = tuple(dict.fromkeys(item for item in normalized if item))
    if not unique:
        return None, False
    if len(unique) > 1:
        return None, True
    return unique[0], False


def _normalize_identifier(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return _VALUE_WHITESPACE.sub(" ", normalized.strip()).casefold()


def _normalize_driver(value: str) -> str:
    normalized = _normalize_identifier(value)
    match = _SQL_SERVER_DRIVER.fullmatch(normalized)
    if match:
        return f"odbc driver {match.group(1)} for sql server"
    dll_match = re.search(r"msodbcsql(\d+)\.dll$", normalized)
    if dll_match:
        return f"odbc driver {dll_match.group(1)} for sql server"
    aliases = {
        "msoledbsql": "microsoft ole db driver for sql server",
        "msoledbsql.1": "microsoft ole db driver for sql server",
        "sqlncli.dll": "sql server native client",
        "sqlncli10.dll": "sql server native client 10.0",
        "sqlncli11.dll": "sql server native client 11.0",
        "sqlsrv32.dll": "sql server",
        "sqloledb": "sql server ole db",
        "sqloledb.1": "sql server ole db",
    }
    return aliases.get(ntpath.basename(normalized), aliases.get(normalized, normalized))


def _normalize_server(value: str) -> str:
    normalized = _normalize_identifier(value)
    for prefix in ("tcp:", "lpc:", "np:"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    if normalized.endswith(",1433"):
        normalized = normalized[:-5]
    return normalized.strip()


def _normalize_file(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value.strip()).replace("/", "\\")
    return ntpath.normpath(normalized).casefold() if normalized else ""


def _normalize_port(value: str) -> str:
    normalized = _normalize_identifier(value)
    if not normalized.isdigit():
        return ""
    number = int(normalized)
    return str(number) if 1 <= number <= 65535 else ""


def _looks_like_file(value: str) -> bool:
    stripped = value.strip()
    return bool(
        stripped.startswith(("\\\\", "//"))
        or re.match(r"^[A-Za-z]:[\\/]", stripped)
        or _WINDOWS_FILE_SUFFIX.search(stripped)
    )
