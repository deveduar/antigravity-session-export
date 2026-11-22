from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from content import extract_from_db
from db import read_conversation_db


def _varint(value: int) -> bytes:
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _bytes_field(number: int, value: bytes) -> bytes:
    return _varint((number << 3) | 2) + _varint(len(value)) + value


def _text_field(number: int, value: str) -> bytes:
    return _bytes_field(number, value.encode("utf-8"))


def _timestamp(seconds: int, nanos: int = 0) -> bytes:
    payload = _varint(8) + _varint(seconds)
    if nanos:
        payload += _varint(16) + _varint(nanos)
    return _bytes_field(5, _bytes_field(1, payload))


def _step(step_type: int, text: str, seconds: int) -> bytes:
    if step_type == 14:
        payload = _bytes_field(19, _text_field(2, text))
    else:
        payload = _bytes_field(20, _text_field(1, text))
    return payload + _timestamp(seconds)


def _planner_step_with_content(text: str, seconds: int) -> bytes:
    call = _text_field(1, "call-1")
    call += _text_field(2, "run_command")
    call += _bytes_field(7, _text_field(2, "db tool result"))
    planner = _text_field(1, text)
    planner += _text_field(3, "db thinking")
    planner += _bytes_field(7, call)
    return _bytes_field(20, planner) + _timestamp(seconds)


class ConversationDbTests(unittest.TestCase):
    def _create_db(self, path: Path) -> None:
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE trajectory_meta (trajectory_id TEXT, cascade_id TEXT)")
            conn.execute("INSERT INTO trajectory_meta VALUES (?, ?)", ("trajectory-1", "session-1"))
            conn.execute("CREATE TABLE trajectory_metadata_blob (id TEXT, data BLOB)")
            conn.execute(
                "INSERT INTO trajectory_metadata_blob VALUES ('main', ?)",
                (_bytes_field(1, _text_field(1, "file:///workspace/project")),),
            )
            conn.execute(
                "CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, step_payload BLOB)"
            )
            conn.executemany(
                "INSERT INTO steps VALUES (?, ?, ?)",
                [
                    (0, 14, _step(14, "Offline question", 1_763_481_600)),
                    (1, 21, b"ignored tool payload"),
                    (2, 15, _step(15, "Offline planner response", 1_763_481_605)),
                ],
            )

    def test_reads_user_and_planner_turns_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "session.db"
            self._create_db(path)
            original = path.read_bytes()

            conversation = read_conversation_db(path)

            self.assertEqual(conversation.session_id, "session-1")
            self.assertEqual(conversation.trajectory_id, "trajectory-1")
            self.assertEqual(conversation.workspace_uri, "file:///workspace/project")
            self.assertEqual(
                [(turn.role, turn.text) for turn in conversation.turns],
                [("user", "Offline question"), ("assistant", "Offline planner response")],
            )
            self.assertEqual(conversation.step_count, 3)
            self.assertEqual(path.read_bytes(), original)

    def test_title_uses_first_non_empty_turn_line(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "session.db"
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, step_payload BLOB)")
                conn.execute(
                    "INSERT INTO steps VALUES (?, ?, ?)",
                    (0, 14, _step(14, "\n\nReal title\nMore text", 1_763_481_600)),
                )

            conversation = read_conversation_db(path)

            self.assertEqual(conversation.title, "Real title")

    def test_rejects_database_without_steps_table(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "invalid.db"
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TABLE other (id INTEGER)")

            with self.assertRaisesRegex(ValueError, "step_type and step_payload"):
                read_conversation_db(path)

    def test_extracts_reasoning_and_linked_tool_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "session.db"
            with sqlite3.connect(path) as conn:
                conn.execute(
                    "CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, step_payload BLOB)"
                )
                conn.execute(
                    "INSERT INTO steps VALUES (?, ?, ?)",
                    (0, 15, _planner_step_with_content("Offline answer", 1_763_481_600)),
                )

            reasoning, tool_calls = extract_from_db(path)

            self.assertEqual(len(reasoning), 1)
            self.assertEqual(reasoning[0].text, "db thinking")
            self.assertEqual(len(tool_calls), 1)
            self.assertEqual(tool_calls[0].name, "run_command")
            self.assertEqual(tool_calls[0].result, "db tool result")


if __name__ == "__main__":
    unittest.main()