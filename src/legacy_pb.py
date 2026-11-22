from __future__ import annotations

from pathlib import Path

from content import extract_content_from_proto_steps, with_content
from db import (
    Conversation,
    ReasoningBlock,
    ToolCall,
    _bytes_field,
    first_line_title,
    _fields,
    _string_field,
    _turn_from_step,
    _varint_field,
)


LEGACY_KEY = b"safeCodeiumworldKeYsecretBalloon"
NONCE_SIZE = 12
TAG_SIZE = 16


class LegacyPbUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__(
            "optional dependency 'cryptography' is not installed; install it in "
            "the exporter's Python with python -m pip install 'cryptography>=41'"
        )


def decrypt_legacy_pb(data: bytes) -> bytes:
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise LegacyPbUnavailable() from exc
    if len(data) < NONCE_SIZE + TAG_SIZE:
        raise ValueError("legacy .pb is truncated")
    try:
        return AESGCM(LEGACY_KEY).decrypt(data[:NONCE_SIZE], data[NONCE_SIZE:], None)
    except InvalidTag as exc:
        raise ValueError("legacy .pb authentication failed") from exc


def _workspace_uri(plain: bytes) -> str:
    metadata = _bytes_field(plain, 7)
    if not metadata:
        return ""
    for raw in _bytes_field(metadata[0], 7):
        candidate = bytes(raw).decode("utf-8", errors="replace")
        if candidate.startswith("file://"):
            return candidate
    for raw in _bytes_field(metadata[0], 1):
        for field in (1, 2):
            values = _bytes_field(bytes(raw), field)
            if values:
                candidate = values[0].decode("utf-8", errors="replace")
                if candidate.startswith("file://"):
                    return candidate
    return ""


def read_legacy_pb_blocks(
    path: Path,
) -> tuple[list[bytes], tuple[ReasoningBlock, ...], tuple[ToolCall, ...]]:
    plain = decrypt_legacy_pb(path.read_bytes())
    list(_fields(plain))
    steps = [value for number, wire, value in _fields(plain) if number == 2 and wire == 2]
    reasoning, tool_calls, _ = extract_content_from_proto_steps(steps)
    return steps, reasoning, tool_calls


def read_legacy_pb(path: Path) -> Conversation:
    plain = decrypt_legacy_pb(path.read_bytes())
    list(_fields(plain))
    steps = [value for number, wire, value in _fields(plain) if number == 2 and wire == 2]
    turns = tuple(
        turn
        for step in steps
        if (turn := _turn_from_step(_varint_field(step, 1), step)) is not None
    )
    session_id = _string_field(plain, 6) or path.stem
    trajectory_id = _string_field(plain, 1)
    title = first_line_title(turns[0].text) if turns else ""
    modified = max((turn.created_at_ms or 0) for turn in turns)
    conversation = Conversation(
        session_id=session_id,
        trajectory_id=trajectory_id,
        title=title,
        workspace_uri=_workspace_uri(plain),
        turns=turns,
        modified_at_ms=modified or int(path.stat().st_mtime * 1000),
        step_count=len(steps),
    )
    reasoning, tool_calls, tool_steps = extract_content_from_proto_steps(steps)
    return with_content(conversation, reasoning, tool_calls, tool_steps)
