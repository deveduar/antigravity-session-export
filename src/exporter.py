from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from version import __version__
from autostart import ManagedLaunch
from content import (
    DEFAULT_PRODUCTS,
    PRODUCT_CHOICES,
    ContentOptions,
    extract_content_from_db,
    extract_tool_steps_from_json_steps,
    _clean_step_text,
    _json_text,
    extract_from_json_steps,
    has_selected_content,
    has_undecipherable_selected_content,
    is_undecipherable_output,
    link_ordered_transcript_results,
    with_content,
)
from db import Conversation, TranscriptRecord, Turn, first_line_title, read_conversation_db
from legacy_pb import LegacyPbUnavailable, read_legacy_pb
from live import fetch_trajectory
from selection import SelectionOptions, apply_selection


UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
LOG_DIR = Path(".system_generated") / "logs"
TRANSCRIPT_NAMES = ("transcript_full.jsonl", "transcript.jsonl")
PRESET_CHOICES = (
    "conversation", "prompts", "markdown", "reasoning", "system",
    "tool-calls", "tool-results", "tool-steps", "complete",
)
DEFAULT_PRESET = "conversation"
SURFACES = ("standard", "ide", "cli")
SURFACE_CHOICES = ("auto", *SURFACES)
SURFACE_PREFERENCE = ("ide", "cli", "standard")


@dataclass(frozen=True)
class SessionSource:
    session_id: str
    surface: str
    path: Path
    title: str = ""
    workspace: str = ""
    transcript: bool = False
    storage_format: str = "pb"


def _infer_surface(root: Path) -> str:
    if root.name == "antigravity-ide":
        return "ide"
    if root.name == "antigravity-cli":
        return "cli"
    return "standard"


def _resolve_surface(surface: str | None, root: Path) -> str:
    value = (surface or "auto").strip().lower()
    if value == "auto":
        return _infer_surface(root)
    if value not in SURFACES:
        raise ValueError(f"unknown surface {value!r}; choose {', '.join(SURFACE_CHOICES)}")
    return value


def _storage_format(path: Path, transcript: bool) -> str:
    if transcript:
        return "transcript_jsonl"
    if path.suffix.lower() == ".db":
        return "db"
    return "pb"


def _roots(
    home: Path,
    brain_override: Path | None = None,
    surface: str | None = None,
) -> list[tuple[str, Path]]:
    if brain_override is not None:
        root = brain_override.parent
        return [(_resolve_surface(surface, root), root)]
    return [
        ("standard", home / ".gemini" / "antigravity"),
        ("ide", home / ".gemini" / "antigravity-ide"),
        ("cli", home / ".gemini" / "antigravity-cli"),
    ]


def _annotations_title(surface_root: Path, session_id: str) -> str:
    annotation = surface_root / "annotations" / f"{session_id}.pbtxt"
    try:
        text = annotation.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    match = re.search(r'\btitle\s*:\s*"((?:\\.|[^"\\])*)"', text)
    if not match:
        return ""
    try:
        return bytes(match.group(1), "utf-8").decode("unicode_escape").strip()
    except (UnicodeDecodeError, ValueError):
        return match.group(1).strip()


def _workspace_uri_from_json(value: Any) -> str:
    if isinstance(value, str):
        return value if value.startswith("file://") else ""
    if isinstance(value, dict):
        for nested in value.values():
            found = _workspace_uri_from_json(nested)
            if found:
                return found
    if isinstance(value, list):
        for nested in value:
            found = _workspace_uri_from_json(nested)
            if found:
                return found
    return ""


def _transcript_workspace(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, raw in enumerate(handle):
                if line_number >= 50:
                    break
                if not raw.strip():
                    continue
                try:
                    step = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                found = _workspace_uri_from_json(step)
                if found:
                    return found
    except OSError:
        pass
    return ""


def _candidate_roots(surface_root: Path) -> list[Path]:
    roots = [surface_root, surface_root / "conversations_backup"]
    if surface_root.name == "antigravity":
        legacy_backup = surface_root.parent / "antigravity-backup"
        if legacy_backup.is_dir():
            roots.append(legacy_backup)
    return [root for root in roots if root.is_dir()]


def discover_sessions(
    home: Path | None = None,
    brain_override: Path | None = None,
    surface: str | None = None,
) -> list[SessionSource]:
    home = home or Path.home()
    catalog: dict[tuple[str, str], SessionSource] = {}
    for surface, surface_root in _roots(home, brain_override, surface):
        for root_index, candidate_root in enumerate(_candidate_roots(surface_root)):
            candidates: dict[str, Path] = {}
            if candidate_root == surface_root:
                for name in TRANSCRIPT_NAMES:
                    for transcript in (candidate_root / "brain").glob(f"*/{LOG_DIR}/{name}"):
                        candidates.setdefault(transcript.parent.parent.parent.name, transcript)
            conversations = candidate_root if candidate_root.name == "conversations_backup" else candidate_root / "conversations"
            if conversations.is_dir():
                for extension in ("pb", "db"):
                    for path in conversations.glob(f"*.{extension}"):
                        if UUID_RE.fullmatch(path.stem):
                            previous = candidates.get(path.stem)
                            if previous is None or (previous.suffix == ".pb" and extension == "db"):
                                candidates[path.stem] = path

            for session_id, path in candidates.items():
                key = (surface, session_id)
                current = catalog.get(key)
                metadata_root = surface_root if candidate_root.name in {"conversations_backup", "antigravity-backup"} else candidate_root
                transcript = path.name in TRANSCRIPT_NAMES
                source = SessionSource(
                    session_id=session_id,
                    surface=surface,
                    path=path,
                    title=_annotations_title(metadata_root, session_id),
                    transcript=transcript,
                    workspace=_transcript_workspace(path) if transcript else "",
                    storage_format=_storage_format(path, transcript),
                )
                if current is None or (root_index == 0 and not current.transcript) or (
                    current.path.suffix == ".pb" and source.path.suffix == ".db"
                ):
                    catalog[key] = source
    return sorted(catalog.values(), key=lambda item: (item.surface, item.session_id))


def _timestamp_ms(value: Any) -> int | None:
    if isinstance(value, dict):
        seconds = value.get("seconds")
        nanos = value.get("nanos", 0)
        try:
            return int(float(seconds) * 1000 + float(nanos or 0) / 1_000_000)
        except (TypeError, ValueError, OverflowError):
            return None
    if isinstance(value, (int, float)):
        return int(value if value > 10_000_000_000 else value * 1000)
    if isinstance(value, str):
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            return int(datetime.fromisoformat(raw).timestamp() * 1000)
        except ValueError:
            try:
                numeric = float(raw)
                return int(numeric if numeric > 10_000_000_000 else numeric * 1000)
            except (ValueError, OverflowError):
                return None
    return None


def _live_conversation(source: SessionSource) -> Conversation:
    trajectory, steps = fetch_trajectory(source.session_id)
    turns: list[Turn] = []
    for step in steps:
        kind = str(step.get("type") or "")
        if kind in {"CORTEX_STEP_TYPE_USER_INPUT", "USER_INPUT"}:
            payload = step.get("userInput") or {}
            role, text = "user", str(payload.get("userResponse") or "").strip()
        elif kind in {"CORTEX_STEP_TYPE_PLANNER_RESPONSE", "PLANNER_RESPONSE"}:
            payload = step.get("plannerResponse") or {}
            role, text = "assistant", str(payload.get("response") or "").strip()
        else:
            continue
        if text:
            turns.append(Turn(role, text, _timestamp_ms(step.get("createdAt") or step.get("created_at"))))
    if not turns:
        raise ValueError("live trajectory contains no user or assistant text")
    title = source.title or str(trajectory.get("summary") or "").strip() or first_line_title(turns[0].text)
    workspaces: list[str] = []
    for workspace in trajectory.get("workspaces", []):
        if isinstance(workspace, dict):
            for key in ("workspaceFolderAbsoluteUri", "gitRootAbsoluteUri"):
                value = workspace.get(key)
                if value:
                    workspaces.append(str(value))
    metadata = trajectory.get("trajectoryMetadata", {})
    if isinstance(metadata, dict):
        workspaces.extend(str(value) for value in metadata.get("workspaceUris", []) if value)
    workspace = ""
    for uri in workspaces:
        if uri.startswith("file://"):
            workspace = unquote(urlparse(uri).path or "")
            if platform_is_windows_path(workspace):
                workspace = workspace[1:]
            break
    dates = [turn.created_at_ms for turn in turns if turn.created_at_ms]
    modified = max(dates, default=int(source.path.stat().st_mtime * 1000))
    conversation = Conversation(source.session_id, str(trajectory.get("trajectoryId") or ""), title,
                                workspace, tuple(turns), modified, len(steps))
    reasoning, tool_calls = extract_from_json_steps(steps)
    tool_steps = extract_tool_steps_from_json_steps(steps)
    return with_content(conversation, reasoning, tool_calls, tool_steps)


def platform_is_windows_path(value: str) -> bool:
    return len(value) >= 3 and value[0] == "/" and value[2] == ":"


def _jsonl_conversation(source: SessionSource) -> Conversation:
    steps: list[dict[str, Any]] = []
    turns: list[Turn] = []
    records: list[TranscriptRecord] = []
    with source.path.open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            try:
                step = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"line {line_number}: invalid JSON ({exc.msg})") from exc
            if not isinstance(step, dict):
                continue
            steps.append(step)
            kind, origin = step.get("type"), step.get("source")
            text = _json_text(step.get("content") or step.get("response") or "")
            timestamp = _timestamp_ms(step.get("created_at") or step.get("createdAt"))
            step_index = step.get("step_index", step.get("stepIndex"))
            step_index = int(step_index) if isinstance(step_index, (int, float)) else None
            status = str(step.get("status") or "")
            if kind == "USER_INPUT" and origin == "USER_EXPLICIT":
                blocks = re.findall(r"<USER_REQUEST>(.*?)</USER_REQUEST>", text, re.S)
                text = "\n\n".join(block.strip() for block in blocks).strip() if blocks else text
                turns.append(Turn("user", text, timestamp, step_index, status))
            elif kind in {"PLANNER_RESPONSE", "CORTEX_STEP_TYPE_PLANNER_RESPONSE"} and text:
                turns.append(Turn("assistant", text, timestamp, step_index, status))
            elif kind in {"SYSTEM_MESSAGE", "GENERIC"}:
                record_type = "system" if kind == "SYSTEM_MESSAGE" else "generic"
                records.append(TranscriptRecord(record_type, text, timestamp, step_index, status))

    reasoning, tool_calls = extract_from_json_steps(steps)
    tool_steps = extract_tool_steps_from_json_steps(steps)
    tool_calls, used_generic = link_ordered_transcript_results(steps, tool_calls)
    records = [
        record for record in records
        if not (record.record_type == "generic" and record.step_index in used_generic)
    ]
    if not turns and not reasoning and not tool_calls and not tool_steps and not records:
        raise ValueError("JSONL transcript contains no exportable records")
    title_source = next(
        (item.text for item in turns + records + list(tool_steps) if item.text.strip()),
        "",
    )
    title = source.title or first_line_title(title_source)
    modified = int(source.path.stat().st_mtime * 1000)
    return Conversation(
        source.session_id, "", title, source.workspace, tuple(turns),
        modified, len(steps), reasoning, tool_calls, tool_steps, tuple(records),
    )


def utc_iso(milliseconds: int | None) -> str:
    if not milliseconds:
        return ""
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _source_stat(source: SessionSource) -> tuple[Any, Any]:
    wal = source.path.with_name(source.path.name + "-wal") if source.path.suffix == ".db" else None
    try:
        stat = source.path.stat()
    except OSError:
        stat = None
    try:
        wal_stat = wal.stat() if wal is not None and wal.exists() else None
    except OSError:
        wal_stat = None
    return stat, wal_stat


def session_revision(source: SessionSource, step_count: int = 0) -> str:
    stat, wal_stat = _source_stat(source)
    return "|".join(
        (
            str(int(stat.st_mtime_ns) if stat else 0),
            str(int(stat.st_size) if stat else 0),
            str(step_count),
            str(int(wal_stat.st_mtime_ns) if wal_stat else 0),
            str(int(wal_stat.st_size) if wal_stat else 0),
            source.title,
            source.workspace,
        )
    )


def _preview_conversation(source: SessionSource) -> Conversation | None:
    try:
        if source.transcript:
            return _jsonl_conversation(source)
        suffix = source.path.suffix.lower()
        if suffix == ".db":
            return read_conversation_db(source.path)
        if suffix == ".pb":
            return read_legacy_pb(source.path)
    except (OSError, ValueError, sqlite3.Error, LegacyPbUnavailable):
        pass
    return None


def session_record(source: SessionSource) -> dict[str, Any]:
    stat, _ = _source_stat(source)
    preview = _preview_conversation(source)
    title = source.title or (preview.title if preview else "")
    step_count = preview.step_count if preview else 0
    workspace = source.workspace or (preview.workspace_uri if preview else "")
    updated_ms = int(stat.st_mtime * 1000) if stat else 0
    return {
        "id": f"{source.surface}:{source.session_id}",
        "session_id": source.session_id,
        "surface": source.surface,
        "storage_format": source.storage_format,
        "title": title,
        "project_directory": workspace,
        "archived": False,
        "step_count": step_count,
        "updated": utc_iso(updated_ms),
        "last_modified_time": str(stat.st_mtime_ns) if stat else "",
        "revision": session_revision(source, step_count),
        "has_transcript": source.transcript,
        "transcript_path": str(source.path) if source.transcript else None,
        "conversation_path": None if source.transcript else str(source.path),
        "source_path": str(source.path),
    }


def list_sessions(
    *,
    home: Path | None = None,
    brain_override: Path | None = None,
    surface: str | None = None,
    selection: SelectionOptions | None = None,
) -> list[dict[str, Any]]:
    records = [
        session_record(source)
        for source in discover_sessions(home=home, brain_override=brain_override, surface=surface)
    ]
    return apply_selection(records, selection)


def _load_conversation(
    source: SessionSource,
    record: dict[str, Any],
    launch: ManagedLaunch,
    content: ContentOptions,
) -> Conversation:
    if source.transcript:
        conversation = _jsonl_conversation(source)
    elif source.path.suffix.lower() == ".db":
        conversation = read_conversation_db(source.path)
        reasoning, tool_calls, tool_steps = extract_content_from_db(source.path)
        conversation = with_content(conversation, reasoning, tool_calls, tool_steps)
    else:
        try:
            conversation = read_legacy_pb(source.path)
        except (OSError, ValueError, LegacyPbUnavailable) as offline_exc:
            launch.ensure()
            try:
                conversation = _live_conversation(source)
            except Exception as live_exc:
                raise ValueError(
                    f"offline .pb decode failed ({offline_exc}); "
                    f"live fallback failed ({live_exc})"
                ) from live_exc
    modified_ms = int(record.get("last_modified_time") or 0) // 1_000_000
    if not modified_ms:
        modified_ms = int(source.path.stat().st_mtime * 1000)
    conversation = replace(
        conversation,
        modified_at_ms=modified_ms,
        title=source.title or conversation.title,
        workspace_uri=source.workspace or conversation.workspace_uri,
    )
    needs_live = not has_selected_content(conversation, content)
    if has_undecipherable_selected_content(conversation, content):
        needs_live = True
    if needs_live:
        try:
            live = _live_conversation(source)
            live = replace(
                live,
                modified_at_ms=conversation.modified_at_ms,
                title=conversation.title,
                workspace_uri=conversation.workspace_uri,
            )
            if (has_selected_content(live, content)
                    and not has_undecipherable_selected_content(live, content)):
                conversation = live
        except Exception:
            pass
    return conversation


def _content_signature(conversation: Conversation, content: ContentOptions) -> str:
    payload: dict[str, Any] = {"content": content.names()}
    if content.prompts or content.answers:
        payload["messages"] = [
            {
                "role": turn.role,
                "created_at": utc_iso(turn.created_at_ms),
                "text": turn.text,
                "step_index": turn.step_index,
                "status": turn.status,
            }
            for turn in conversation.turns
            if (turn.role == "user" and content.prompts) or (turn.role == "assistant" and content.answers)
        ]
    if content.system:
        payload["system_messages"] = [
            {"created_at": utc_iso(item.created_at_ms), "text": item.text,
             "step_index": item.step_index, "status": item.status}
            for item in conversation.transcript_records if item.record_type == "system"
        ]
    if content.reasoning:
        reasoning_items: list[dict[str, Any]] = []
        for block in conversation.reasoning:
            item: dict[str, Any] = {
                "created_at": utc_iso(block.created_at_ms), "text": block.text,
                "signature": block.signature, "step_index": block.step_index,
                "status": block.status,
            }
            if content.include_raw:
                item["raw_text"] = block.raw_text
            reasoning_items.append(item)
        payload["reasoning"] = reasoning_items
    if content.tool_calls:
        payload["tool_calls"] = [
            {"id": call.id, "name": call.name, "arguments": call.arguments,
             "created_at": utc_iso(call.created_at_ms), "step_index": call.step_index,
             "status": call.status, "linked_by": call.linked_by}
            for call in conversation.tool_calls
        ]
    if content.tool_results:
        payload["tool_results"] = [
            {"id": call.id, "name": call.name, "result": call.result,
             "created_at": utc_iso(call.created_at_ms), "step_index": call.step_index,
             "status": call.status, "linked_by": call.linked_by}
            for call in conversation.tool_calls
            if call.result and not call.result_encrypted and not is_undecipherable_output(call.result)
        ]
        payload["generic_records"] = [
            {"created_at": utc_iso(item.created_at_ms), "text": item.text,
             "step_index": item.step_index, "status": item.status}
            for item in conversation.transcript_records
            if item.record_type == "generic" and item.text and not is_undecipherable_output(item.text)
        ]
    if content.tool_steps:
        payload["tool_steps"] = [
            {"name": item.name, "text": item.text, "created_at": utc_iso(item.created_at_ms),
             "step_index": item.step_index, "status": item.status, "step_type": item.step_type}
            for item in conversation.tool_steps
            if item.text and not is_undecipherable_output(item.text)
        ]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _surface_rank(surface: str) -> int:
    return SURFACE_PREFERENCE.index(surface) if surface in SURFACE_PREFERENCE else len(SURFACE_PREFERENCE)


def _dedupe_decoded(
    entries: list[tuple[dict[str, Any], SessionSource, Conversation | None, str | None]],
    content: ContentOptions,
) -> list[tuple[dict[str, Any], SessionSource, Conversation | None, str | None]]:
    groups: dict[str, list[tuple[dict[str, Any], SessionSource, Conversation | None, str | None]]] = {}
    for entry in entries:
        groups.setdefault(entry[1].session_id, []).append(entry)
    keep: set[int] = set()
    for group in groups.values():
        if len(group) == 1:
            keep.add(id(group[0]))
            continue
        by_signature: dict[str, list[tuple[dict[str, Any], SessionSource, Conversation | None, str | None]]] = {}
        undecoded: list[tuple[dict[str, Any], SessionSource, Conversation | None, str | None]] = []
        for entry in group:
            conversation = entry[2]
            if conversation is None:
                undecoded.append(entry)
                continue
            signature = _content_signature(conversation, content)
            by_signature.setdefault(signature, []).append(entry)
        for candidates in by_signature.values():
            candidates.sort(key=lambda item: (_surface_rank(item[1].surface), item[0]["id"]))
            keep.add(id(candidates[0]))
        keep.update(id(entry) for entry in undecoded)
    return [entry for entry in entries if id(entry) in keep]


def _fenced(text: str) -> list[str]:
    value = text.rstrip()
    marker = "````" if "```" in value else "```"
    return [marker, value, marker]


def _render(conversation: Conversation, surface: str, content: ContentOptions | None = None) -> str:
    content = content or ContentOptions()
    title = _clean_step_text(conversation.title) or "untitled"
    date_text = datetime.fromtimestamp(conversation.modified_at_ms / 1000, tz=timezone.utc).date().isoformat()
    fields = [
        "---",
        "source: antigravity",
        f"surface: {json.dumps(surface, ensure_ascii=False)}",
        f"session_id: {json.dumps(conversation.session_id, ensure_ascii=False)}",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        f"date: {json.dumps(date_text)}",
        f"message_count: {len(conversation.turns)}",
    ]
    if conversation.workspace_uri:
        fields.append(f"project_directory: {json.dumps(_clean_step_text(conversation.workspace_uri), ensure_ascii=False)}")
    fields.extend(["---", "", f"# {title}", ""])
    for turn in conversation.turns:
        if turn.role == "user" and not content.prompts:
            continue
        if turn.role == "assistant" and not content.answers:
            continue
        heading = "User" if turn.role == "user" else "Assistant"
        stamp = utc_iso(turn.created_at_ms)
        fields.append(f"## {heading} — {stamp}" if stamp else f"## {heading}")
        fields.extend(["", _clean_step_text(turn.text).rstrip(), ""])

    if content.system:
        for record in conversation.transcript_records:
            if record.record_type != "system":
                continue
            stamp = utc_iso(record.created_at_ms)
            fields.append(f"## System — {stamp}" if stamp else "## System")
            fields.extend(["", _clean_step_text(record.text).rstrip(), ""])

    if content.reasoning:
        if conversation.reasoning:
            for block in conversation.reasoning:
                stamp = utc_iso(block.created_at_ms)
                fields.append(f"## Reasoning — {stamp}" if stamp else "## Reasoning")
                fields.append("")
                if block.text:
                    fields.extend([_clean_step_text(block.text).rstrip(), ""])
                if content.include_raw and block.raw_text:
                    fields.extend(["### Raw thinking", "", *_fenced(_clean_step_text(block.raw_text)), ""])

    if content.tool_calls:
        if conversation.tool_calls:
            for call in conversation.tool_calls:
                stamp = utc_iso(call.created_at_ms)
                fields.append(f"## Tool call — {stamp}" if stamp else "## Tool call")
                fields.extend([
                    "",
                    f"ID: {call.id or '(none)'}",
                    f"Name: {call.name or '(none)'}",
                    "",
                ])
                if call.arguments:
                    fields.extend(["### Arguments", "", *_fenced(_clean_step_text(call.arguments)), ""])

    if content.tool_results:
        linked = [
            call for call in conversation.tool_calls
            if call.result and not is_undecipherable_output(call.result)
        ]
        if linked:
            for call in linked:
                stamp = utc_iso(call.created_at_ms)
                fields.append(f"## Tool result — {stamp}" if stamp else "## Tool result")
                fields.extend([
                    "",
                    f"ID: {call.id or '(none)'}",
                    f"Name: {call.name or '(none)'}",
                    "",
                    "### Result",
                    "",
                    *_fenced(_clean_step_text(call.result)),
                    "",
                ])
        for record in conversation.transcript_records:
            if record.record_type != "generic" or is_undecipherable_output(record.text):
                continue
            stamp = utc_iso(record.created_at_ms)
            fields.append(f"## Tool output — {stamp}" if stamp else "## Tool output")
            fields.extend(["", _clean_step_text(record.text).rstrip(), ""])
    if content.tool_steps:
        if conversation.tool_steps:
            for item in conversation.tool_steps:
                if is_undecipherable_output(item.text):
                    continue
                stamp = utc_iso(item.created_at_ms)
                heading = "Tool step"
                if item.name:
                    heading += f" — {_clean_step_text(item.name)}"
                fields.append(f"## {heading} — {stamp}" if stamp else f"## {heading}")
                fields.extend(["", _clean_step_text(item.text).rstrip(), ""])
    return "\n".join(fields).rstrip() + "\n"


def _sanitize_record(value: Any) -> Any:
    if isinstance(value, str):
        return _clean_step_text(value)
    if isinstance(value, list):
        return [_sanitize_record(item) for item in value]
    if isinstance(value, dict):
        return {key: _sanitize_record(item) for key, item in value.items()}
    return value


def _jsonl_line(record: dict[str, Any]) -> str:
    line = json.dumps(_sanitize_record(record), ensure_ascii=False, separators=(",", ":"))
    line = line.replace("\u0085", "\\u0085").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return line + "\n"


def _session_base(conversation: Conversation, surface: str, source_path: Path) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "tool": "antigravity_export",
        "surface": surface,
        "session_id": conversation.session_id,
        "trajectory_id": conversation.trajectory_id,
        "title": conversation.title,
        "date": datetime.fromtimestamp(conversation.modified_at_ms / 1000, tz=timezone.utc).date().isoformat(),
        "modified_at": utc_iso(conversation.modified_at_ms),
        "workspace_uri": conversation.workspace_uri,
        "source_path": str(source_path),
        "step_count": conversation.step_count,
    }


def _render_memory(conversation: Conversation, surface: str, source_path: Path, content: ContentOptions) -> str:
    record: dict[str, Any] = {**_session_base(conversation, surface, source_path), "record_type": "session"}
    if content.prompts or content.answers:
        record["messages"] = [
            {
                "role": turn.role,
                "created_at": utc_iso(turn.created_at_ms),
                "text": turn.text,
                "step_index": turn.step_index,
                "status": turn.status,
            }
            for turn in conversation.turns
            if (turn.role == "user" and content.prompts) or (turn.role == "assistant" and content.answers)
        ]
    if content.system:
        record["system_messages"] = [
            {"created_at": utc_iso(item.created_at_ms), "text": item.text,
             "step_index": item.step_index, "status": item.status}
            for item in conversation.transcript_records if item.record_type == "system"
        ]
    if content.reasoning:
        reasoning_items: list[dict[str, Any]] = []
        for block in conversation.reasoning:
            item: dict[str, Any] = {
                "created_at": utc_iso(block.created_at_ms), "text": block.text,
                "step_index": block.step_index, "status": block.status,
            }
            if content.include_raw:
                item["raw_text"] = block.raw_text
            reasoning_items.append(item)
        record["reasoning"] = reasoning_items
    if content.tool_calls:
        record["tool_calls"] = [
            {"id": call.id, "name": call.name, "arguments": call.arguments,
             "created_at": utc_iso(call.created_at_ms), "step_index": call.step_index,
             "status": call.status, "linked_by": call.linked_by}
            for call in conversation.tool_calls
        ]
    if content.tool_results:
        record["tool_results"] = [
            {"id": call.id, "name": call.name, "result": call.result,
             "created_at": utc_iso(call.created_at_ms), "step_index": call.step_index,
             "status": call.status, "linked_by": call.linked_by}
            for call in conversation.tool_calls
            if call.result and not is_undecipherable_output(call.result)
        ]
        record["generic_records"] = [
            {"created_at": utc_iso(item.created_at_ms), "text": item.text,
             "step_index": item.step_index, "status": item.status}
            for item in conversation.transcript_records
            if item.record_type == "generic" and not is_undecipherable_output(item.text)
        ]
    if content.tool_steps:
        record["tool_steps"] = [
            {"name": item.name, "text": item.text, "created_at": utc_iso(item.created_at_ms),
             "step_index": item.step_index, "status": item.status, "step_type": item.step_type}
            for item in conversation.tool_steps
            if not is_undecipherable_output(item.text)
        ]
    return _jsonl_line(record)


def _corpus_text(conversation: Conversation, content: ContentOptions) -> str:
    parts: list[str] = [f"# {conversation.title}"]
    for item in conversation.transcript_records:
        if content.system and item.record_type == "system":
            parts.extend(["System:", _clean_step_text(item.text).rstrip(), ""])
    for turn in conversation.turns:
        if turn.role == "user" and not content.prompts:
            continue
        if turn.role == "assistant" and not content.answers:
            continue
        parts.extend([turn.role.capitalize() + ":", _clean_step_text(turn.text).rstrip(), ""])
    for block in conversation.reasoning:
        if content.reasoning and block.text:
            parts.extend(["Reasoning:", _clean_step_text(block.text).rstrip(), ""])
    for call in conversation.tool_calls:
        if content.tool_calls:
            parts.extend([f"Tool call: {call.name or '(none)'}", call.arguments.rstrip(), ""])
        if content.tool_results and call.result and not is_undecipherable_output(call.result):
            parts.extend([f"Tool result: {call.name or '(none)'}", call.result.rstrip(), ""])
    for item in conversation.transcript_records:
        if (content.tool_results and item.record_type == "generic"
                and not is_undecipherable_output(item.text)):
            parts.extend(["Tool output:", _clean_step_text(item.text).rstrip(), ""])
    for item in conversation.tool_steps:
        if content.tool_steps and not is_undecipherable_output(item.text):
            parts.extend([f"Tool step: {_clean_step_text(item.name) or '(none)'}", _clean_step_text(item.text).rstrip(), ""])
    return "\n".join(parts).strip() + "\n"


def _render_corpus(conversation: Conversation, surface: str, source_path: Path, content: ContentOptions) -> str:
    text = _clean_step_text(_corpus_text(conversation, content))
    record = {
        **_session_base(conversation, surface, source_path),
        "record_type": "corpus_document",
        "id": f"{surface}:{conversation.session_id}",
        "content": content.names(),
        "char_count": len(text),
        "text": text,
    }
    return _jsonl_line(record)


def _filename(title: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "_", title).strip("_")[:80] or "untitled"
    return safe


def _session_date(conversation: Conversation) -> str:
    return datetime.fromtimestamp(conversation.modified_at_ms / 1000, tz=timezone.utc).date().isoformat()


def _safe_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "run"


def _unique_run_name(output_dir: Path, run_label: str) -> str:
    base = f"{utc_stamp()}_{_safe_component(run_label)}"
    candidate = base
    suffix = 2
    while (output_dir / candidate).exists():
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


def _project_id(conversation: Conversation) -> str:
    if not conversation.workspace_uri:
        return "_no-project"
    digest = hashlib.sha256(conversation.workspace_uri.encode("utf-8")).hexdigest()
    return digest[:12]


def _project_title(conversation: Conversation) -> str:
    workspace = conversation.workspace_uri
    if not workspace:
        return "_no-project"
    if workspace.startswith("file://"):
        workspace = unquote(urlparse(workspace).path)
    else:
        workspace = unquote(workspace)
    parts = [part for part in re.split(r"[\\/]+", workspace.rstrip("\\/")) if part]
    return _safe_component(parts[-1]) if parts else "_no-project"


def _output_filename(
    conversation: Conversation,
    source: SessionSource,
    preset: str,
    group_dir_name: str,
    suffix: str = ".md",
) -> Path:
    return Path(group_dir_name) / (
        f"{_session_date(conversation)}_"
        f"{_filename(conversation.title)}_"
        f"{source.surface}.{preset}{suffix}"
    )


def _unique_relative_path(path: Path, used: set[str]) -> str:
    candidate = path
    counter = 2
    while candidate.as_posix() in used:
        candidate = candidate.with_name(f"{candidate.stem}_{counter}{candidate.suffix}")
        counter += 1
    used.add(candidate.as_posix())
    return candidate.as_posix()


def _product_filename(product: str) -> str:
    return "memory.jsonl" if product == "memory" else "corpus.jsonl"


def export_all(
    output_dir: Path,
    *,
    home: Path | None = None,
    brain_override: Path | None = None,
    surface: str | None = None,
    run_label: str = "conversation",
    preset: str = DEFAULT_PRESET,
    selection: SelectionOptions | None = None,
    dry_run: bool = False,
    autostart: bool = False,
    content: ContentOptions | None = None,
    products: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    content = content or ContentOptions()
    products = tuple(dict.fromkeys(products or DEFAULT_PRODUCTS))
    if preset not in PRESET_CHOICES:
        raise ValueError(f"unknown preset {preset!r}")
    for product in products:
        if product not in PRODUCT_CHOICES:
            raise ValueError(f"unknown product {product!r}")

    sources = discover_sessions(home=home, brain_override=brain_override, surface=surface)
    records = apply_selection([session_record(source) for source in sources], selection)
    by_key = {(source.surface, source.session_id): source for source in sources}
    run_name = ""
    run_dir: Path | None = None
    if dry_run:
        run_name = _unique_run_name(output_dir, run_label)
        run_dir = output_dir / run_name
    counts: dict[str, dict[str, int]] = {}
    warnings: list[dict[str, str]] = []
    metadata_sessions: list[dict[str, Any]] = []
    product_names = {
        product: _product_filename(product)
        for product in products if product in {"memory", "corpus"}
    }
    product_lines: dict[str, list[str]] = {product: [] for product in product_names}
    markdown_outputs: list[tuple[str, str]] = []
    used_paths: set[str] = set()
    exported = failed = 0
    decoded: list[tuple[dict[str, Any], SessionSource, Conversation | None, str | None]] = []

    launch = ManagedLaunch(autostart)
    try:
        for record in records:
            source = by_key[(record["surface"], record["session_id"])]
            source_record = session_record(source)
            try:
                conversation = _load_conversation(source, source_record, launch, content)
                if not has_selected_content(conversation, content):
                    raise ValueError("conversation has no exportable selected content")
                error = None
            except Exception as exc:
                conversation = None
                error = f"{type(exc).__name__}: {exc}"
            decoded.append((source_record, source, conversation, error))
        decoded = _dedupe_decoded(decoded, content)
        group_dates: dict[tuple[str, str], str] = {}
        for _, _, conversation, error in decoded:
            if error is not None:
                continue
            key = (_project_title(conversation), _project_id(conversation))
            date = _session_date(conversation)
            group_dates[key] = max(group_dates.get(key, date), date)
        for record, source, conversation, error in decoded:
            key = source.surface
            result = counts.setdefault(key, {"scanned": 0, "exported": 0, "failed": 0})
            result["scanned"] += 1
            if error is not None:
                failed += 1
                result["failed"] += 1
                warning = {"surface": key, "session_id": source.session_id, "error": error}
                warnings.append(warning)
                metadata_sessions.append({**record, "status": "failed", "error": error})
                continue
            if dry_run:
                exported += 1
                result["exported"] += 1
                metadata_sessions.append({**record, "status": "would_export"})
                continue

            session_output_files: list[str] = []
            for product in products:
                if product == "memory":
                    product_lines["memory"].append(
                        _render_memory(conversation, source.surface, source.path, content)
                    )
                    session_output_files.append(product_names["memory"])
                elif product == "corpus":
                    product_lines["corpus"].append(
                        _render_corpus(conversation, source.surface, source.path, content)
                    )
                    session_output_files.append(product_names["corpus"])
                else:
                    group_key = (_project_title(conversation), _project_id(conversation))
                    project_title = _safe_component(group_key[0])
                    project_id = _safe_component(group_key[1])
                    project_part = project_title if project_title == project_id else f"{project_title}_{project_id}"
                    group_name = f"{group_dates[group_key]}_{project_part}"
                    relative = _output_filename(
                        conversation, source, preset,
                        group_dir_name=group_name, suffix=".md",
                    )
                    output_file = _unique_relative_path(relative, used_paths)
                    markdown_outputs.append((output_file, _render(conversation, source.surface, content)))
                    session_output_files.append(output_file)
            exported += 1
            result["exported"] += 1
            session_entry: dict[str, Any] = {**record, "status": "exported",
                                             "output_file": session_output_files[0]}
            if len(session_output_files) > 1:
                session_entry["output_files"] = session_output_files
            metadata_sessions.append(session_entry)
    finally:
        launch.close()

    if not dry_run and exported == 0:
        return {
            "source": "antigravity",
            "run_id": "",
            "run_dir": "",
            "scanned": len(decoded),
            "exported": exported,
            "failed": failed,
            "surfaces": counts,
            "warnings": warnings,
        }

    if not dry_run:
        run_name = _unique_run_name(output_dir, run_label)
        run_dir = output_dir / run_name
        run_dir.mkdir(parents=True, exist_ok=True)
        for relative, text in markdown_outputs:
            path = run_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        for product, lines in product_lines.items():
            (run_dir / product_names[product]).write_text("".join(lines), encoding="utf-8")
        metadata = {
            "schema_version": 1,
            "tool": "antigravity_export",
            "version": __version__,
            "run_id": run_name,
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "run_label": run_label,
            "preset": preset,
            "selection": selection.as_dict() if selection else {},
            "selection_key": selection.key() if selection else "",
            "counts": {"exported": exported, "failed": failed, "surfaces": counts},
            "warnings": warnings,
            "sessions": metadata_sessions,
        }
        if products != DEFAULT_PRODUCTS:
            metadata["products"] = list(products)
        if not content.is_default:
            metadata["content"] = content.names()
            if content.include_raw:
                metadata["include_raw_reasoning"] = True
        (run_dir / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return {
        "source": "antigravity",
        "run_id": run_name,
        "run_dir": str(run_dir) if run_dir is not None else "",
        "scanned": len(decoded),
        "exported": exported,
        "failed": failed,
        "surfaces": counts,
        "warnings": warnings,
    }
