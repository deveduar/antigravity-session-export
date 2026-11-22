from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class SelectionOptions:
    last: int | None = None
    since: str = ""
    sessions: tuple[str, ...] = ()
    filter_text: str = ""
    order: str = "updated-desc"

    def as_dict(self) -> dict[str, Any]:
        return {
            "last": self.last,
            "since": self.since,
            "sessions": list(self.sessions),
            "filter": self.filter_text,
            "order": self.order,
        }

    def key(self) -> str:
        payload = json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _parse_ms(value: Any) -> int:
    raw = str(value or "").strip()
    if not raw:
        return 0
    if raw.isdigit() and len(raw) <= 13:
        return int(raw)
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def apply_selection(
    sessions: list[dict[str, Any]],
    options: SelectionOptions | None = None,
) -> list[dict[str, Any]]:
    if options is None:
        return list(sessions)
    selected = list(sessions)
    if options.sessions:
        wanted = {item.strip().lower() for item in options.sessions if item.strip()}
        selected = [
            session for session in selected
            if str(session.get("id", "")).lower() in wanted
            or str(session.get("session_id", "")).lower() in wanted
        ]
    if options.filter_text:
        needle = options.filter_text.casefold()
        selected = [
            session for session in selected
            if needle in " ".join(
                str(session.get(key) or "")
                for key in ("title", "session_id", "surface", "project_directory", "source_path")
            ).casefold()
        ]
    since_ms = _parse_ms(options.since)
    if since_ms:
        selected = [session for session in selected if _parse_ms(session.get("updated")) >= since_ms]
    ordered = sorted(
        selected,
        key=lambda session: (_parse_ms(session.get("updated")), str(session.get("id", ""))),
    )
    if options.order.endswith("desc"):
        ordered.reverse()
    if options.last is not None:
        ordered = ordered[: options.last]
    return ordered
