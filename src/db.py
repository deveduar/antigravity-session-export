from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


USER_INPUT_STEP = 14
PLANNER_RESPONSE_STEP = 15


def first_line_title(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:120]
    return ""


@dataclass(frozen=True)
class Turn:
    role: str
    text: str
    created_at_ms: int | None
    step_index: int | None = None
    status: str = ""


@dataclass(frozen=True)
class ReasoningBlock:
    text: str
    raw_text: str
    signature: str
    created_at_ms: int | None
    step_index: int | None = None
    status: str = ""


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str
    result: str
    created_at_ms: int | None
    step_index: int | None = None
    status: str = ""
    linked_by: str = ""
    result_encrypted: bool = False


@dataclass(frozen=True)
class ToolStep:
    name: str
    text: str
    created_at_ms: int | None
    step_index: int | None = None
    status: str = ""
    step_type: int | None = None


@dataclass(frozen=True)
class TranscriptRecord:
    record_type: str
    text: str
    created_at_ms: int | None
    step_index: int | None = None
    status: str = ""


@dataclass(frozen=True)
class Conversation:
    session_id: str
    trajectory_id: str
    title: str
    workspace_uri: str
    turns: tuple[Turn, ...]
    modified_at_ms: int
    step_count: int
    reasoning: tuple[ReasoningBlock, ...] = ()
    tool_calls: tuple[ToolCall, ...] = ()
    tool_steps: tuple[ToolStep, ...] = ()
    transcript_records: tuple[TranscriptRecord, ...] = ()


def _sqlite_uri(path: Path) -> str:
    absolute = path.resolve()
    uri = absolute.as_uri()
    return f"{uri}?mode=ro"


def _decode_varint(data: bytes, offset: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while offset < len(data) and shift < 70:
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, offset
        shift += 7
    raise ValueError("invalid or truncated protobuf varint")


def _fields(data: bytes):
    offset = 0
    while offset < len(data):
        tag, offset = _decode_varint(data, offset)
        number, wire_type = tag >> 3, tag & 7
        if number == 0:
            raise ValueError("protobuf field number cannot be zero")
        if wire_type == 0:
            value, offset = _decode_varint(data, offset)
            yield number, wire_type, value
        elif wire_type == 1:
            end = offset + 8
            if end > len(data):
                raise ValueError("truncated protobuf fixed64 field")
            yield number, wire_type, data[offset:end]
            offset = end
        elif wire_type == 2:
            length, offset = _decode_varint(data, offset)
            end = offset + length
            if end > len(data):
                raise ValueError("truncated protobuf bytes field")
            yield number, wire_type, data[offset:end]
            offset = end
        elif wire_type == 5:
            end = offset + 4
            if end > len(data):
                raise ValueError("truncated protobuf fixed32 field")
            yield number, wire_type, data[offset:end]
            offset = end
        else:
            raise ValueError(f"unsupported protobuf wire type {wire_type}")


def _varint_field(data: bytes, field_number: int) -> int:
    return next((value for number, wire, value in _fields(data)
                 if number == field_number and wire == 0), 0)


def _bytes_field(data: bytes, field_number: int) -> list[bytes]:
    return [value for number, wire, value in _fields(data) if number == field_number and wire == 2]


def _string_field(data: bytes, field_number: int) -> str:
    values = _bytes_field(data, field_number)
    return values[0].decode("utf-8", errors="replace") if values else ""


def _step_time_ms(step_payload: bytes) -> int | None:
    metadata = _bytes_field(step_payload, 5)
    if not metadata:
        return None
    created_at = _bytes_field(metadata[0], 1)
    if not created_at:
        return None
    seconds = 0
    nanos = 0
    for number, wire, value in _fields(created_at[0]):
        if number == 1 and wire == 0:
            seconds = int(value)
        elif number == 2 and wire == 0:
            nanos = int(value)
    return seconds * 1000 + nanos // 1_000_000 if seconds else None


def _turn_from_step(step_type: int, payload: bytes) -> Turn | None:
    if step_type == USER_INPUT_STEP:
        nested_field, text_fields, role = 19, (2, 1), "user"
    elif step_type == PLANNER_RESPONSE_STEP:
        nested_field, text_fields, role = 20, (1, 8), "assistant"
    else:
        return None

    nested = _bytes_field(payload, nested_field)
    if not nested:
        return None
    text = next((_string_field(nested[0], field).strip() for field in text_fields
                 if _string_field(nested[0], field).strip()), "")
    if not text:
        return None
    return Turn(role=role, text=text, created_at_ms=_step_time_ms(payload))


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _find_steps_table(conn: sqlite3.Connection) -> tuple[str, set[str]]:
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        quoted = _quote_identifier(str(name))
        columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({quoted})")}
        if {"step_type", "step_payload"}.issubset(columns):
            return quoted, columns
    raise ValueError("conversation DB has no table with step_type and step_payload")


def _read_workspace_uri(conn: sqlite3.Connection) -> str:
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "trajectory_metadata_blob" not in tables:
        return ""
    try:
        row = conn.execute("SELECT data FROM trajectory_metadata_blob WHERE id='main'").fetchone()
    except sqlite3.Error:
        return ""
    if not row or row[0] is None:
        return ""
    raw = row[0]
    if isinstance(raw, str):
        raw = raw.encode("utf-8", errors="surrogateescape")
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        return ""
    try:
        outer_fields = list(_fields(bytes(raw)))
    except ValueError:
        return ""
    workspace_uri = ""
    for number, wire, value in outer_fields:
        if number == 7 and wire == 2:
            workspace_uri = bytes(value).decode("utf-8", errors="replace")
        elif number == 1 and wire == 2:
            try:
                nested = list(_fields(bytes(value)))
            except ValueError:
                continue
            for nested_number, nested_wire, nested_value in nested:
                if nested_number in {1, 2} and nested_wire == 2:
                    candidate = bytes(nested_value).decode("utf-8", errors="replace")
                    if candidate.startswith("file://"):
                        workspace_uri = candidate
                        break
    return workspace_uri


def read_conversation_db(path: Path) -> Conversation:
    """Read an Antigravity SQLite conversation without modifying its source."""
    if not path.is_file():
        raise FileNotFoundError(path)

    conn = sqlite3.connect(_sqlite_uri(path), uri=True, timeout=5)
    try:
        conn.execute("PRAGMA query_only=ON")
        table, columns = _find_steps_table(conn)
        order_by = _quote_identifier("idx") if "idx" in columns else "rowid"
        rows = conn.execute(
            f"SELECT step_type, step_payload FROM {table} ORDER BY {order_by}"
        )
        turns = tuple(
            turn
            for step_type, payload in rows
            if isinstance(payload, (bytes, bytearray, memoryview))
            and (turn := _turn_from_step(int(step_type), bytes(payload))) is not None
        )

        trajectory_id = ""
        session_id = path.stem
        workspace_uri = ""
        if "trajectory_meta" in {
            str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }:
            meta_columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(trajectory_meta)")
            }
            selected = [name for name in ("trajectory_id", "cascade_id") if name in meta_columns]
            if selected:
                values = conn.execute(
                    "SELECT " + ", ".join(_quote_identifier(name) for name in selected)
                    + " FROM trajectory_meta LIMIT 1"
                ).fetchone()
                if values:
                    metadata = dict(zip(selected, values))
                    trajectory_id = str(metadata.get("trajectory_id") or "")
                    session_id = str(metadata.get("cascade_id") or session_id)

                workspace_uri = _read_workspace_uri(conn)
        title = first_line_title(turns[0].text) if turns else ""
        latest = max((turn.created_at_ms or 0) for turn in turns)
        step_count = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        return Conversation(
            session_id=session_id,
            trajectory_id=trajectory_id,
            title=title,
            workspace_uri=workspace_uri,
            turns=turns,
            modified_at_ms=latest or int(path.stat().st_mtime * 1000),
            step_count=step_count,
        )
    finally:
        conn.close()