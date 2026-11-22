from __future__ import annotations

import base64
import binascii
import json
import re
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from db import (
    PLANNER_RESPONSE_STEP,
    Conversation,
    ReasoningBlock,
    ToolCall,
    ToolStep,
    TranscriptRecord,
    _bytes_field,
    _decode_varint,
    _find_steps_table,
    _quote_identifier,
    _sqlite_uri,
    _step_time_ms,
    _string_field,
    _varint_field,
)


CONTENT_CHOICES = ("prompts", "answers", "reasoning", "system", "tool-calls", "tool-results", "tool-steps")
PRODUCT_CHOICES = ("markdown", "memory", "corpus")
DEFAULT_PRODUCTS = ("markdown",)
_DEFAULT_CONTENT = ("prompts", "answers", "reasoning")
_ENCRYPTED_RESULT_MAGICS = (
    bytes.fromhex("01be3ef6fb"),
    bytes.fromhex("0172c8da7c"),
)
_OPAQUE_DIRECT_RE = re.compile(
    r"\$[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.I,
)
_OPAQUE_REFERENCE_RE = re.compile(
    r"\$?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.I,
)
_TOOL_STEP_TYPES = {5, 7, 8, 9, 21, 23, 28, 81, 82, 90}


@dataclass(frozen=True)
class ContentOptions:
    prompts: bool = True
    answers: bool = True
    reasoning: bool = False
    system: bool = False
    tool_calls: bool = False
    tool_results: bool = False
    tool_steps: bool = False
    include_raw: bool = False

    @classmethod
    def from_values(
        cls,
        values: list[str] | tuple[str, ...] | None,
        include_raw: bool = False,
    ) -> ContentOptions:
        selected = set(_DEFAULT_CONTENT if values is None else values)
        if include_raw:
            selected.add("reasoning")
        return cls(
            prompts="prompts" in selected,
            answers="answers" in selected,
            reasoning="reasoning" in selected,
            system="system" in selected,
            tool_calls="tool-calls" in selected,
            tool_results="tool-results" in selected,
            tool_steps="tool-steps" in selected,
            include_raw=include_raw and "reasoning" in selected,
        )

    @property
    def is_default(self) -> bool:
        return (
            self.prompts
            and self.answers
            and self.reasoning
            and not self.system
            and not self.tool_calls
            and not self.tool_results
            and not self.tool_steps
            and not self.include_raw
        )

    def names(self) -> list[str]:
        return [
            name
            for name, enabled in (
                ("prompts", self.prompts),
                ("answers", self.answers),
                ("reasoning", self.reasoning),
                ("system", self.system),
                ("tool-calls", self.tool_calls),
                ("tool-results", self.tool_results),
                ("tool-steps", self.tool_steps),
            )
            if enabled
        ]


def _first_string(data: bytes, fields: tuple[int, ...]) -> str:
    for field in fields:
        value = _string_field(data, field).strip()
        if value:
            return value
    return ""


def _tool_result_bytes(call: bytes) -> bytes:
    for wrapper in _bytes_field(call, 7):
        for field in (2, 1):
            values = _bytes_field(wrapper, field)
            if values and values[0].strip():
                return values[0]
    return b""


def _envelope_magic(raw: bytes) -> bytes | None:
    candidates = [raw]
    if raw.startswith(b"\x12"):
        try:
            length, offset = _decode_varint(raw, 1)
        except ValueError:
            return None
        candidates.append(raw[offset:offset + length])
    for candidate in candidates:
        if not candidate.startswith(b"\x0a"):
            continue
        try:
            length, offset = _decode_varint(candidate, 1)
        except ValueError:
            continue
        payload = candidate[offset:offset + length]
        for magic in _ENCRYPTED_RESULT_MAGICS:
            if payload.startswith(magic):
                return magic
    return None


def is_encrypted_output(value: str | bytes) -> bool:
    if isinstance(value, str):
        text = value.strip()
        raw = text.encode("utf-8")
    else:
        text = ""
        raw = value
    if _envelope_magic(raw) is not None:
        return True
    if not text:
        return False
    padded = text + "=" * (-len(text) % 4)
    try:
        decoded = base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError):
        return False
    return _envelope_magic(decoded) is not None


def is_opaque_reference(value: str | bytes) -> bool:
    if isinstance(value, str):
        text = value.strip()
    else:
        text = value.decode("utf-8", errors="replace").strip()
    if _OPAQUE_DIRECT_RE.fullmatch(text):
        return True
    if not isinstance(value, str):
        return False
    padded = text + "=" * (-len(text) % 4)
    try:
        decoded = base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError):
        return False
    return _OPAQUE_REFERENCE_RE.search(decoded.decode("utf-8", errors="replace")) is not None


def is_undecipherable_output(value: str | bytes) -> bool:
    return is_encrypted_output(value) or is_opaque_reference(value)


def _is_undecipherable_result(raw: bytes) -> bool:
    return (
        is_undecipherable_output(raw)
        or is_undecipherable_output(raw.decode("utf-8", errors="replace"))
    )


def _tool_result(call: bytes) -> str:
    raw = _tool_result_bytes(call)
    if not raw or _is_undecipherable_result(raw):
        return ""
    text = raw.decode("utf-8", errors="replace").strip()
    if "\x00" in text or "\ufffd" in text:
        return ""
    return text


def parse_planner_payload(payload: bytes) -> tuple[ReasoningBlock | None, tuple[ToolCall, ...]]:
    nested = _bytes_field(payload, 20)
    if not nested:
        return None, ()
    planner = nested[0]
    thinking = _first_string(planner, (3,))
    raw_thinking = _first_string(planner, (16,))
    signature = _first_string(planner, (14,))
    timestamp = _step_time_ms(payload)
    reasoning = None
    if thinking or raw_thinking or signature:
        reasoning = ReasoningBlock(thinking, raw_thinking, signature, timestamp)
    tool_calls: list[ToolCall] = []
    for raw_call in _bytes_field(planner, 7):
        call_id = _first_string(raw_call, (1,))
        name = _first_string(raw_call, (2,))
        arguments = _first_string(raw_call, (3,))
        result = _tool_result(raw_call)
        undecipherable = _is_undecipherable_result(_tool_result_bytes(raw_call))
        if call_id or name or arguments or result or undecipherable:
            tool_calls.append(ToolCall(call_id, name, arguments, result, timestamp,
                                       result_encrypted=undecipherable))
    return reasoning, tuple(tool_calls)


def extract_from_planner_payloads(
    payloads: Iterable[bytes],
) -> tuple[tuple[ReasoningBlock, ...], tuple[ToolCall, ...]]:
    reasoning: list[ReasoningBlock] = []
    tool_calls: list[ToolCall] = []
    for payload in payloads:
        block, calls = parse_planner_payload(payload)
        if block is not None:
            reasoning.append(block)
        tool_calls.extend(calls)
    return tuple(reasoning), tuple(tool_calls)


def extract_from_proto_steps(
    steps: Iterable[bytes],
) -> tuple[tuple[ReasoningBlock, ...], tuple[ToolCall, ...]]:
    planner_steps = [step for step in steps if _varint_field(step, 1) == PLANNER_RESPONSE_STEP]
    return extract_from_planner_payloads(planner_steps)


def _tool_step_metadata(step: bytes) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for number, wire, metadata in _safe_fields(step):
        if number != 5 or wire != 2 or not isinstance(metadata, bytes):
            continue
        for nested_number, nested_wire, payload in _safe_fields(metadata):
            if nested_number != 4 or nested_wire != 2 or not isinstance(payload, bytes):
                continue
            record = {
                "call_id": _safe_string_field(payload, 1),
                "name": _safe_string_field(payload, 2),
                "arguments": _clean_step_text(_safe_string_field(payload, 3)),
                "result": _clean_step_text(_safe_string_field(payload, 6)),
            }
            if any(record.values()):
                records.append(record)
    return records


def link_proto_tool_results(
    steps: Iterable[bytes],
    tool_calls: tuple[ToolCall, ...],
) -> tuple[tuple[ToolCall, ...], set[int]]:
    candidates: dict[str, list[tuple[int, dict[str, str]]]] = {}
    for index, step in enumerate(steps):
        step_type = next((value for number, wire, value in _safe_fields(step)
                          if number == 1 and wire == 0), 0)
        if step_type not in _TOOL_STEP_TYPES:
            continue
        for record in _tool_step_metadata(step):
            if record["call_id"] and record["result"]:
                candidates.setdefault(record["call_id"], []).append((index, record))
    linked_indices: set[int] = set()
    updated: list[ToolCall] = []
    for call in tool_calls:
        matches = candidates.get(call.id, []) if call.id else []
        match = next(((index, record) for index, record in matches
                      if index not in linked_indices), matches[0] if matches else None)
        if match is None:
            updated.append(call)
            continue
        index, record = match
        linked_indices.add(index)
        updated.append(replace(
            call,
            name=call.name or record["name"],
            arguments=call.arguments or record["arguments"],
            result=call.result or record["result"],
            result_encrypted=False if not call.result else call.result_encrypted,
            linked_by=call.linked_by or "call_id",
        ))
    return tuple(updated), linked_indices


def extract_content_from_proto_steps(
    steps: Iterable[bytes],
) -> tuple[tuple[ReasoningBlock, ...], tuple[ToolCall, ...], tuple[ToolStep, ...]]:
    materialized = list(steps)
    reasoning, tool_calls = extract_from_proto_steps(materialized)
    tool_calls, linked_indices = link_proto_tool_results(materialized, tool_calls)
    tool_steps = extract_tool_steps_from_proto_steps(materialized, linked_indices)
    return reasoning, tool_calls, tool_steps


def _read_db_steps(path: Path) -> list[tuple[int, bytes]]:
    conn = sqlite3.connect(_sqlite_uri(path), uri=True, timeout=5)
    try:
        conn.execute("PRAGMA query_only=ON")
        table, columns = _find_steps_table(conn)
        order_by = _quote_identifier("idx") if "idx" in columns else _quote_identifier("rowid")
        rows = conn.execute(
            f"SELECT step_type, step_payload FROM {table} ORDER BY {order_by}"
        )
        return [(int(step_type), bytes(payload)) for step_type, payload in rows
                if isinstance(payload, (bytes, bytearray, memoryview))]
    finally:
        conn.close()


def extract_from_db(path: Path) -> tuple[tuple[ReasoningBlock, ...], tuple[ToolCall, ...]]:
    planner_steps = [step for step_type, step in _read_db_steps(path)
                     if step_type == PLANNER_RESPONSE_STEP]
    return extract_from_planner_payloads(planner_steps)


def extract_tool_steps_from_db(path: Path) -> tuple[ToolStep, ...]:
    return extract_tool_steps_from_proto_steps([step for _, step in _read_db_steps(path)])


def extract_content_from_db(
    path: Path,
) -> tuple[tuple[ReasoningBlock, ...], tuple[ToolCall, ...], tuple[ToolStep, ...]]:
    return extract_content_from_proto_steps([step for _, step in _read_db_steps(path)])


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, dict):
        for key in ("text", "value", "response", "content", "userResponse"):
            if key in value:
                text = _json_text(value[key])
                if text:
                    return text
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, list):
        return "\n".join(text for text in (_json_text(item) for item in value) if text)
    return ""


def _json_first(data: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        text = _json_text(data.get(key))
        if text:
            return text
    return ""


def _json_timestamp(value: Any) -> int | None:
    if isinstance(value, dict):
        seconds, nanos = value.get("seconds"), value.get("nanos", 0)
        try:
            return int(float(seconds) * 1000 + float(nanos or 0) / 1_000_000)
        except (TypeError, ValueError, OverflowError):
            return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value if value > 10_000_000_000 else value * 1000)
    if isinstance(value, str):
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            return int(datetime.fromisoformat(raw).timestamp() * 1000)
        except ValueError:
            return None
    return None


def _json_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def extract_from_json_steps(
    steps: Iterable[dict[str, Any]],
) -> tuple[tuple[ReasoningBlock, ...], tuple[ToolCall, ...]]:
    reasoning: list[ReasoningBlock] = []
    tool_calls: list[ToolCall] = []
    for step in steps:
        kind = str(step.get("type") or "")
        planner = step.get("plannerResponse")
        nested = isinstance(planner, dict)
        if not nested and kind not in {"CORTEX_STEP_TYPE_PLANNER_RESPONSE", "PLANNER_RESPONSE"}:
            continue
        payload = planner if nested else step
        thinking = _json_first(payload, ("thinking",))
        raw_thinking = _json_first(payload, ("rawThinking", "raw_thinking"))
        signature = _json_first(payload, ("thinkingSignature", "thinking_signature", "signature"))
        timestamp = _json_timestamp(step.get("createdAt") or step.get("created_at"))
        step_index = _json_int(step.get("step_index") or step.get("stepIndex"))
        status = str(step.get("status") or "")
        if thinking or raw_thinking or signature:
            reasoning.append(ReasoningBlock(thinking, raw_thinking, signature, timestamp,
                                            step_index, status))
        raw_calls = (
            planner.get("toolCalls") or planner.get("tool_calls") or []
            if nested else step.get("tool_calls") or step.get("toolCalls") or []
        )
        if not isinstance(raw_calls, list):
            continue
        for raw_call in raw_calls:
            if not isinstance(raw_call, dict):
                continue
            call_id = _json_first(raw_call, ("id", "toolCallId", "tool_call_id", "call_id"))
            name = _json_first(raw_call, ("name", "toolName", "tool_name"))
            arguments = _json_first(raw_call, ("args", "arguments", "argumentsJson"))
            result = _json_first(raw_call, ("result", "toolResult", "tool_result", "response"))
            if call_id or name or arguments or result:
                tool_calls.append(ToolCall(call_id, name, arguments, result, timestamp,
                                           step_index, status))
    return tuple(reasoning), tuple(tool_calls)


def _safe_fields(data: bytes):
    offset = 0
    while offset < len(data):
        try:
            key, offset = _decode_varint(data, offset)
            number, wire = key >> 3, key & 7
            if number == 0:
                return
            if wire == 0:
                value, offset = _decode_varint(data, offset)
            elif wire == 1:
                end = offset + 8
                if end > len(data):
                    return
                value, offset = data[offset:end], end
            elif wire == 2:
                length, offset = _decode_varint(data, offset)
                end = offset + length
                if end > len(data):
                    return
                value, offset = data[offset:end], end
            elif wire == 5:
                end = offset + 4
                if end > len(data):
                    return
                value, offset = data[offset:end], end
            else:
                return
        except ValueError:
            return
        yield number, wire, value


def _safe_string_field(data: bytes, field_number: int) -> str:
    for number, wire, value in _safe_fields(data):
        if number == field_number and wire == 2 and isinstance(value, bytes):
            return value.decode("utf-8", errors="replace").strip()
    return ""


def _clean_step_text(text: str) -> str:
    value = text.strip()
    if not value or "\x00" in value or "\ufffd" in value:
        return ""
    printable = sum(1 for char in value if char.isprintable() or char in "\r\n\t")
    return value if printable / len(value) >= 0.9 else ""


def extract_tool_steps_from_proto_steps(
    steps: Iterable[bytes],
    linked_indices: set[int] | None = None,
) -> tuple[ToolStep, ...]:
    linked_indices = linked_indices or set()
    tool_steps: list[ToolStep] = []
    for index, step in enumerate(steps):
        if index in linked_indices:
            continue
        fields = list(_safe_fields(step))
        step_type = next((value for number, wire, value in fields if number == 1 and wire == 0), 0)
        if step_type not in _TOOL_STEP_TYPES:
            continue
        metadata = _tool_step_metadata(step)
        if metadata:
            record = metadata[0]
            best = record["result"] or record["arguments"]
            name = record["name"]
            if best:
                try:
                    timestamp = _step_time_ms(step)
                except ValueError:
                    timestamp = None
                tool_steps.append(ToolStep(name, best, timestamp, index, "", step_type))
                continue
        name = ""
        best = ""
        for number, wire, payload in fields:
            if wire != 2 or number == 5 or not isinstance(payload, bytes):
                continue
            if not name:
                name = _safe_string_field(payload, 1)
            nested_best = ""
            for nested_number, nested_wire, nested_value in _safe_fields(payload):
                if nested_wire != 2 or not isinstance(nested_value, bytes):
                    continue
                text = _clean_step_text(nested_value.decode("utf-8", errors="replace"))
                if len(text) > len(nested_best):
                    nested_best = text
            if nested_best:
                best = nested_best if len(nested_best) > len(best) else best
            else:
                direct = _clean_step_text(payload.decode("utf-8", errors="replace"))
                if len(direct) > len(best):
                    best = direct
        if best:
            try:
                timestamp = _step_time_ms(step)
            except ValueError:
                timestamp = None
            tool_steps.append(ToolStep(name, best, timestamp, index, "", step_type))
    return tuple(tool_steps)


def extract_tool_steps_from_json_steps(steps: Iterable[dict[str, Any]]) -> tuple[ToolStep, ...]:
    tool_steps: list[ToolStep] = []
    for index, step in enumerate(steps):
        kind = str(step.get("type") or "")
        if isinstance(step.get("plannerResponse"), dict):
            continue
        if kind in {"CORTEX_STEP_TYPE_USER_INPUT", "USER_INPUT",
                    "CORTEX_STEP_TYPE_PLANNER_RESPONSE", "PLANNER_RESPONSE"}:
            continue
        text = _clean_step_text(_json_first(step, ("text", "content", "response", "output", "result", "message")))
        if not text:
            continue
        name = _json_first(step, ("name", "toolName", "tool", "type"))
        tool_steps.append(ToolStep(
            name,
            text,
            _json_timestamp(step.get("createdAt") or step.get("created_at")),
            _json_int(step.get("step_index") or step.get("stepIndex")) or index,
            str(step.get("status") or ""),
            None,
        ))
    return tuple(tool_steps)


def link_ordered_transcript_results(
    steps: Iterable[dict[str, Any]],
    tool_calls: tuple[ToolCall, ...],
) -> tuple[tuple[ToolCall, ...], set[int | None]]:
    grouped: dict[int, list[ToolCall]] = {}
    for call in tool_calls:
        if call.step_index is not None:
            grouped.setdefault(call.step_index, []).append(call)
    if not grouped:
        return tool_calls, set()

    results: dict[int, list[tuple[str, int | None, int | None, str]]] = {}
    current: int | None = None
    for step in steps:
        kind = str(step.get("type") or "")
        index = _json_int(step.get("step_index") or step.get("stepIndex"))
        if kind in {"CORTEX_STEP_TYPE_PLANNER_RESPONSE", "PLANNER_RESPONSE"}:
            current = index if index in grouped else None
            continue
        if kind in {"CORTEX_STEP_TYPE_USER_INPUT", "USER_INPUT"}:
            current = None
            continue
        if kind != "GENERIC" or current is None:
            continue
        text = _json_first(step, ("content", "text", "response"))
        if not text:
            continue
        results.setdefault(current, []).append((
            text,
            _json_timestamp(step.get("createdAt") or step.get("created_at")),
            index,
            str(step.get("status") or ""),
        ))

    consumed: dict[int, int] = {}
    linked_steps: set[int | None] = set()
    updated: list[ToolCall] = []
    for call in tool_calls:
        index = call.step_index
        values = results.get(index, []) if index is not None else []
        offset = consumed.get(index, 0) if index is not None else 0
        if index is not None and offset < len(values):
            text, timestamp, step_index, status = values[offset]
            consumed[index] = offset + 1
            linked_steps.add(step_index)
            if not call.result:
                updated.append(replace(call, result=text, created_at_ms=call.created_at_ms or timestamp,
                                       linked_by="order", status=call.status or status,
                                       step_index=call.step_index if call.step_index is not None else step_index))
                continue
        updated.append(call)
    return tuple(updated), linked_steps


def with_content(
    conversation: Conversation,
    reasoning: tuple[ReasoningBlock, ...],
    tool_calls: tuple[ToolCall, ...],
    tool_steps: tuple[ToolStep, ...] = (),
) -> Conversation:
    return Conversation(
        session_id=conversation.session_id,
        trajectory_id=conversation.trajectory_id,
        title=conversation.title,
        workspace_uri=conversation.workspace_uri,
        turns=conversation.turns,
        modified_at_ms=conversation.modified_at_ms,
        step_count=conversation.step_count,
        reasoning=reasoning or conversation.reasoning,
        tool_calls=tool_calls or conversation.tool_calls,
        tool_steps=tool_steps or conversation.tool_steps,
        transcript_records=conversation.transcript_records,
    )


def has_undecipherable_selected_content(
    conversation: Conversation,
    options: ContentOptions,
) -> bool:
    if options.tool_results:
        if any(call.result_encrypted or is_undecipherable_output(call.result)
               for call in conversation.tool_calls):
            return True
        if any(is_undecipherable_output(record.text)
               for record in conversation.transcript_records
               if record.record_type == "generic"):
            return True
    if options.tool_steps and any(is_undecipherable_output(step.text) for step in conversation.tool_steps):
        return True
    return False


def has_selected_content(conversation: Conversation, options: ContentOptions) -> bool:
    if options.prompts and any(turn.role == "user" for turn in conversation.turns):
        return True
    if options.answers and any(turn.role == "assistant" for turn in conversation.turns):
        return True
    if options.reasoning and conversation.reasoning:
        return True
    if options.system and any(record.record_type == "system" for record in conversation.transcript_records):
        return True
    if options.tool_calls and any(
        call.id or call.name or call.arguments for call in conversation.tool_calls
    ):
        return True
    if options.tool_results and (
        any(
            call.result and not call.result_encrypted and not is_undecipherable_output(call.result)
            for call in conversation.tool_calls
        )
        or any(
            record.record_type == "generic"
            and record.text
            and not is_undecipherable_output(record.text)
            for record in conversation.transcript_records
        )
    ):
        return True
    if options.tool_steps and any(
        step.text and not is_undecipherable_output(step.text) for step in conversation.tool_steps
    ):
        return True
    return False
