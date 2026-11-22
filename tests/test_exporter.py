from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from cli import main
from content import ContentOptions
from db import read_conversation_db
from exporter import SessionSource, discover_sessions, export_all, list_sessions
from live import _parse_process
from selection import SelectionOptions

from test_db import _step
from test_legacy_pb import _write_pb


SID = "12345678-1234-1234-1234-123456789abc"


class ExporterTests(unittest.TestCase):
    def _write_db(self, path: Path, sid: str = SID, question: str = "Question", answer: str = "Answer") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE trajectory_meta (trajectory_id TEXT, cascade_id TEXT)")
            conn.execute("INSERT INTO trajectory_meta VALUES (?, ?)", ("trajectory-1", sid))
            conn.execute(
                "CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, step_payload BLOB)"
            )
            conn.executemany(
                "INSERT INTO steps VALUES (?, ?, ?)",
                [(0, 14, _step(14, question, 1_763_481_600)),
                 (1, 15, _step(15, answer, 1_763_481_605))],
            )

    def _conversations(self, home: Path) -> Path:
        return home / ".gemini" / "antigravity" / "conversations"

    def _brain(self, home: Path) -> Path:
        return home / ".gemini" / "antigravity" / "brain"

    @staticmethod
    def _mtime(path: Path, day: int = 10) -> None:
        timestamp = datetime(2026, 11, day, 12, tzinfo=timezone.utc).timestamp()
        os.utime(path, (timestamp, timestamp))

    @staticmethod
    def _write_transcript(path: Path, title: str = "Antigravity plan") -> None:
        logs = path.parent
        session_dir = logs.parent.parent
        surface_root = session_dir.parent.parent
        annotations = surface_root / "annotations"
        annotations.mkdir(parents=True, exist_ok=True)
        (annotations / f"{SID}.pbtxt").write_text(f'title: "{title}"\n', encoding="utf-8")
        steps = [
            {"step_index": 0, "source": "SYSTEM", "type": "SYSTEM_MESSAGE", "status": "DONE",
             "created_at": "2026-11-14T19:12:20Z",
             "workspace_uri": "file:///workspace/project",
             "content": "Workspace file:///workspace/project is ready"},
            {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
             "created_at": "2026-11-14T19:12:21Z", "content": "inspecting files",
             "thinking": "need to inspect files",
             "tool_calls": [{"name": "read_file", "args": {"Path": "/workspace/project/app.py"}}]},
            {"step_index": 2, "source": "TOOL", "type": "GENERIC", "status": "DONE",
             "created_at": "2026-11-14T19:12:22Z", "content": "file contents"},
            {"step_index": 3, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE",
             "created_at": "2026-11-14T19:12:23Z",
             "content": "<USER_REQUEST>please continue</USER_REQUEST>"},
            {"step_index": 4, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
             "created_at": "2026-11-14T19:12:24Z",
             "tool_calls": [{"name": "wait", "args": {"WaitMsBeforeAsync": 42}}]},
            {"step_index": 5, "source": "TOOL", "type": "GENERIC", "status": "DONE",
             "created_at": "2026-11-14T19:12:25Z", "content": "wait complete"},
        ]
        logs.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(step, ensure_ascii=False) + "\n" for step in steps),
            encoding="utf-8",
        )

    def test_offline_db_export_creates_run_with_iso_headings_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            source = self._conversations(home) / f"{SID}.db"
            self._write_db(source)
            self._mtime(source)
            output = home / "exports" / "antigravity"

            with patch("exporter.fetch_trajectory", side_effect=AssertionError):
                result = export_all(output, home=home)

            self.assertEqual((result["exported"], result["failed"]), (1, 0))
            run_dir = output / result["run_id"]
            self.assertTrue(re.fullmatch(r"\d{8}T\d{6}Z_conversation", result["run_id"]))
            markdown_path = next(run_dir.rglob("*.md"))
            self.assertTrue(markdown_path.parent.name.startswith("2026-11-10_"))
            self.assertTrue(markdown_path.name.endswith(".conversation.md"))
            markdown = markdown_path.read_text(encoding="utf-8")
            self.assertIn('date: "2026-11-10"', markdown)
            self.assertIsNotNone(re.search(r"^## User — \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", markdown, re.M))
            self.assertIsNotNone(re.search(r"^## Assistant — \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", markdown, re.M))
            self.assertIn("Question", markdown)
            self.assertIn("Answer", markdown)
            metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["tool"], "antigravity_export")
            self.assertEqual(metadata["preset"], "conversation")
            self.assertEqual(metadata["sessions"][0]["surface"], "standard")
            self.assertEqual(metadata["sessions"][0]["status"], "exported")
            self.assertTrue((run_dir / metadata["sessions"][0]["output_file"]).is_file())

    def test_markdown_names_include_project_and_session_titles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            source_path = self._conversations(home) / f"{SID}.db"
            self._write_db(source_path)
            self._mtime(source_path)
            source = SessionSource(
                SID, "standard", source_path,
                title="Plan title", workspace="file:///workspace/My%20Project/my-app",
            )

            with patch("exporter.discover_sessions", return_value=[source]), patch(
                "exporter.fetch_trajectory", side_effect=AssertionError
            ):
                result = export_all(home / "out", home=home)

            markdown_path = next((home / "out" / result["run_id"]).rglob("*.md"))
            self.assertRegex(markdown_path.parent.name, r"^2026-11-10_my-app_[0-9a-f]{12}$")
            self.assertEqual(markdown_path.name, "2026-11-10_Plan_title_standard.conversation.md")

    def test_sessions_without_workspace_group_under_single_no_project_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            other = "aaaaaaaa-1234-1234-1234-123456789abc"
            first_path = self._conversations(home) / f"{SID}.db"
            second_path = self._conversations(home) / f"{other}.db"
            self._write_db(first_path)
            self._write_db(second_path, other)
            self._mtime(first_path)
            self._mtime(second_path)
            sources = [
                SessionSource(SID, "standard", first_path, title="First title"),
                SessionSource(other, "standard", second_path, title="Second title"),
            ]

            with patch("exporter.discover_sessions", return_value=sources), patch(
                "exporter.fetch_trajectory", side_effect=AssertionError
            ):
                result = export_all(home / "out", home=home)

            markdown_paths = sorted((home / "out" / result["run_id"]).rglob("*.md"))
            self.assertEqual((result["exported"], result["failed"]), (2, 0))
            self.assertEqual({path.parent.name for path in markdown_paths}, {"2026-11-10_no-project"})

    def test_same_project_group_uses_newest_session_date_in_folder_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            other = "aaaaaaaa-1234-1234-1234-123456789abc"
            first_path = self._conversations(home) / f"{SID}.db"
            second_path = self._conversations(home) / f"{other}.db"
            self._write_db(first_path)
            self._write_db(second_path, other)
            self._mtime(first_path, day=10)
            self._mtime(second_path, day=14)
            workspace = "file:///workspace/My%20Project/my-app"
            sources = [
                SessionSource(SID, "standard", first_path, title="First title", workspace=workspace),
                SessionSource(other, "standard", second_path, title="Second title", workspace=workspace),
            ]

            with patch("exporter.discover_sessions", return_value=sources), patch(
                "exporter.fetch_trajectory", side_effect=AssertionError
            ):
                result = export_all(home / "out", home=home)

            markdown_paths = sorted((home / "out" / result["run_id"]).rglob("*.md"))
            self.assertEqual((result["exported"], result["failed"]), (2, 0))
            self.assertEqual(len({path.parent.name for path in markdown_paths}), 1)
            self.assertRegex(markdown_paths[0].parent.name, r"^2026-11-14_my-app_[0-9a-f]{12}$")
            self.assertEqual(
                [path.name for path in markdown_paths],
                ["2026-11-10_First_title_standard.conversation.md",
                 "2026-11-14_Second_title_standard.conversation.md"],
            )

    def test_each_export_creates_a_new_run_and_no_state_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            self._write_db(self._conversations(home) / f"{SID}.db")
            output = home / "exports" / "antigravity"

            first = export_all(output, home=home)
            second = export_all(output, home=home)

            self.assertNotEqual(first["run_id"], second["run_id"])
            self.assertEqual(len(list(output.glob("*/metadata.json"))), 2)
            self.assertFalse(list(home.rglob("state.json")))
            self.assertFalse(list(output.glob(".*")))

    def test_same_second_run_name_gets_numeric_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            self._write_db(self._conversations(home) / f"{SID}.db")
            output = home / "exports" / "antigravity"

            with patch("exporter.utc_stamp", return_value="20261119T120000Z"):
                first = export_all(output, home=home)
                second = export_all(output, home=home)

            self.assertEqual(first["run_id"], "20261119T120000Z_conversation")
            self.assertEqual(second["run_id"], "20261119T120000Z_conversation_2")

    def test_selection_last_filters_by_updated_time(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            older = "aaaaaaaa-1234-1234-1234-123456789abc"
            newer = "bbbbbbbb-1234-1234-1234-123456789abc"
            old_path = self._conversations(home) / f"{older}.db"
            new_path = self._conversations(home) / f"{newer}.db"
            self._write_db(old_path, older, "Old question", "Old answer")
            self._write_db(new_path, newer, "New question", "New answer")
            os.utime(old_path, (1_700_000_000, 1_700_000_000))
            os.utime(new_path, (1_800_000_000, 1_800_000_000))
            output = home / "exports" / "antigravity"

            result = export_all(output, home=home, selection=SelectionOptions(last=1))

            self.assertEqual(result["scanned"], 1)
            markdown = next((output / result["run_id"]).rglob("*.md")).read_text(encoding="utf-8")
            self.assertIn("New question", markdown)
            self.assertNotIn("Old question", markdown)

    def test_db_is_used_offline_even_without_language_server(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            self._write_db(self._conversations(home) / f"{SID}.db")
            with patch("exporter.fetch_trajectory", side_effect=AssertionError):
                sessions = discover_sessions(home=home)
                self.assertEqual(len(sessions), 1)
                self.assertEqual(read_conversation_db(sessions[0].path).turns[0].text, "Question")

    def test_pb_export_uses_live_rpc_without_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            cache = self._conversations(home)
            cache.mkdir(parents=True)
            (cache / f"{SID}.pb").write_bytes(b"opaque legacy PB")
            output = home / "exports" / "antigravity"
            fake_steps = [
                {"type": "CORTEX_STEP_TYPE_USER_INPUT", "userInput": {"userResponse": "Live question"}},
                {"type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE", "plannerResponse": {"response": "Live answer"}},
            ]
            with patch("exporter.fetch_trajectory",
                       return_value=({"trajectoryId": "trajectory-1"}, fake_steps)) as fetch:
                result = export_all(output, home=home)

            markdown = next((output / result["run_id"]).rglob("*.md")).read_text(encoding="utf-8")
            self.assertIn("Live question", markdown)
            self.assertIn("Live answer", markdown)
            self.assertEqual((result["exported"], result["failed"]), (1, 0))
            fetch.assert_called_once()

    def test_pb_rpc_failure_is_recorded_in_run_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            cache = self._conversations(home)
            cache.mkdir(parents=True)
            (cache / f"{SID}.pb").write_bytes(b"unreadable")
            output = home / "exports" / "antigravity"

            with patch("exporter.fetch_trajectory",
                       side_effect=RuntimeError("server closed")):
                result = export_all(output, home=home)

            self.assertEqual((result["exported"], result["failed"]), (0, 1))
            self.assertEqual(result["run_id"], "")
            self.assertFalse(output.exists())
            self.assertIn("server closed", result["warnings"][0]["error"])

    def test_windows_language_server_flags_are_case_insensitive(self) -> None:
        server = _parse_process(
            42,
            "language_server_windows_x64.exe",
            r"D:\Antigravity\language_server_windows_x64.exe "
            "--CSRF_TOKEN=token-value --WORKSPACE_ID file_D_Project "
            "--EXTENSION_SERVER_PORT 61234",
        )

        self.assertIsNotNone(server)
        self.assertEqual(server.csrf_token, "token-value")
        self.assertEqual(server.workspace_id, "file_D_Project")
        self.assertEqual(server.extension_port, 61234)

    def test_check_cli_emits_native_envelope_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            self._write_db(self._conversations(home) / f"{SID}.db")
            output = home / "exports" / "antigravity"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                status = main(["--check", "--json", "--output", str(output),
                               "--antigravity-dir", str(self._brain(home))])

            payload = json.loads(stdout.getvalue())
            self.assertEqual(status, 0)
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["tool"], "antigravity_export")
            self.assertFalse(payload["writes"])
            self.assertEqual(payload["status"], "ready")
            self.assertEqual(payload["sessions"][0]["id"], f"standard:{SID}")
            self.assertEqual(payload["sessions"][0]["title"], "Question")
            self.assertTrue(payload["sessions"][0]["revision"])
            self.assertRegex(payload["sessions"][0]["updated"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
            self.assertFalse(output.exists())

    def test_list_sessions_recovers_title_from_transcript_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            transcript = self._brain(home) / SID / ".system_generated" / "logs" / "transcript.jsonl"
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.write_text(
                json.dumps({
                    "step_index": 0,
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "status": "DONE",
                    "created_at": "2026-11-14T19:12:20Z",
                    "content": "<USER_REQUEST>Transcript title\nMore text</USER_REQUEST>",
                }, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            sessions = list_sessions(home=home)

            self.assertEqual(sessions[0]["title"], "Transcript title")

    def test_list_sessions_recovers_title_from_legacy_pb(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            source = self._conversations(home) / f"{SID}.pb"
            source.parent.mkdir(parents=True, exist_ok=True)
            _write_pb(source, SID)

            sessions = list_sessions(home=home)

            self.assertEqual(sessions[0]["title"], "hola mundo")

    def test_check_cli_honors_surface_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            self._write_db(self._conversations(home) / f"{SID}.db")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                status = main(["--check", "--json", "--antigravity-dir",
                               str(self._brain(home)), "--surface", "ide"])

            payload = json.loads(stdout.getvalue())
            self.assertEqual(status, 0)
            self.assertEqual(payload["sessions"][0]["id"], f"ide:{SID}")

    def test_check_cli_no_data_returns_three(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                status = main(["--check", "--json", "--antigravity-dir",
                               str(Path(temp_dir) / "missing-brain")])

            payload = json.loads(stdout.getvalue())
            self.assertEqual(status, 3)
            self.assertEqual(payload["status"], "no_data")
            self.assertEqual(payload["sessions"], [])
            self.assertTrue(payload["no_data_reason"])

    def test_cli_sessions_filter(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            other = "aaaaaaaa-1234-1234-1234-123456789abc"
            self._write_db(self._conversations(home) / f"{SID}.db")
            self._write_db(self._conversations(home) / f"{other}.db", other)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                status = main(["--list-sessions", "--json", "--sessions", f"standard:{SID}",
                               "--antigravity-dir", str(self._brain(home))])

            payload = json.loads(stdout.getvalue())
            self.assertEqual(status, 0)
            self.assertEqual([session["session_id"] for session in payload["sessions"]], [SID])

    def test_script_entrypoint_imports_package_when_run_directly(self) -> None:
        script = Path(__file__).parents[1] / "export_sessions.py"
        result = subprocess.run(
            [sys.executable, str(script), "--check", "--json",
             "--antigravity-dir", str(Path(tempfile.gettempdir()) / "no-antigravity-brain")],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 3, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["tool"], "antigravity_export")
        self.assertEqual(payload["sessions"], [])

    def test_backup_pb_ids_are_catalogued_without_primary_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            primary = self._conversations(home)
            backup = home / ".gemini" / "antigravity-backup" / "conversations"
            primary.mkdir(parents=True)
            backup.mkdir(parents=True)
            other = "aaaaaaaa-1234-1234-1234-123456789abc"
            (primary / f"{SID}.pb").write_bytes(b"primary")
            (backup / f"{SID}.pb").write_bytes(b"backup")
            (backup / f"{other}.pb").write_bytes(b"backup only")

            sessions = discover_sessions(home=home)

            self.assertEqual({item.session_id for item in sessions}, {other, SID})
            self.assertEqual(next(item for item in sessions if item.session_id == SID).path.read_bytes(), b"primary")

    def test_duplicate_session_id_across_surfaces_prefers_ide_for_identical_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            other = "aaaaaaaa-1234-1234-1234-123456789abc"
            surface_2 = self._conversations(home)
            surface_ide = home / ".gemini" / "antigravity-ide" / "conversations"
            surface_cli = home / ".gemini" / "antigravity-cli" / "conversations"
            for path in (surface_2, surface_ide, surface_cli):
                path.mkdir(parents=True)
            for path in (surface_2, surface_ide, surface_cli):
                self._write_db(path / f"{SID}.db", question="Shared question", answer="Shared answer")
            self._write_db(surface_ide / f"{other}.db", question="Other question", answer="Other answer")

            result = export_all(home / "out", home=home)
            metadata = json.loads((home / "out" / result["run_id"] / "metadata.json").read_text(encoding="utf-8"))

            self.assertEqual(result["scanned"], 2)
            self.assertEqual(
                [(item["surface"], item["session_id"]) for item in metadata["sessions"]],
                [("ide", SID), ("ide", other)],
            )

    def test_same_session_id_with_different_content_is_not_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            surface_2 = self._conversations(home)
            surface_ide = home / ".gemini" / "antigravity-ide" / "conversations"
            surface_cli = home / ".gemini" / "antigravity-cli" / "conversations"
            for path in (surface_2, surface_ide):
                self._write_db(path / f"{SID}.db", question="Shared question", answer="Shared answer")
            self._write_db(surface_cli / f"{SID}.db", question="Different question", answer="Different answer")

            result = export_all(home / "out", home=home)
            metadata = json.loads((home / "out" / result["run_id"] / "metadata.json").read_text(encoding="utf-8"))

            self.assertEqual(result["scanned"], 2)
            self.assertEqual(
                [(item["surface"], item["session_id"]) for item in metadata["sessions"]],
                [("cli", SID), ("ide", SID)],
            )

    def test_legacy_surface_alias_is_not_accepted_by_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            self._write_db(self._conversations(home) / f"{SID}.db")

            self.assertEqual(
                list_sessions(home=home, selection=SelectionOptions(sessions=(f"2:{SID}",))),
                [],
            )
            records = list_sessions(home=home, selection=SelectionOptions(sessions=(f"standard:{SID}",)))
            self.assertEqual([record["id"] for record in records], [f"standard:{SID}"])

    def test_transcript_full_flat_schema_exports_memory_and_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            logs = self._brain(home) / SID / ".system_generated" / "logs"
            regular = logs / "transcript.jsonl"
            full = logs / "transcript_full.jsonl"
            self._write_transcript(regular, "regular title")
            self._write_transcript(full)
            self._mtime(full)
            output = home / "exports" / "antigravity"
            content = ContentOptions.from_values(
                ["prompts", "answers", "reasoning", "system", "tool-calls", "tool-results"]
            )

            result = export_all(
                output,
                home=home,
                run_label="complete",
                preset="complete",
                content=content,
                products=("markdown", "memory", "corpus"),
            )

            self.assertEqual((result["exported"], result["failed"]), (1, 0))
            run_dir = output / result["run_id"]
            markdown_path = next(run_dir.rglob("*.md"))
            self.assertTrue(markdown_path.parent.name.startswith("2026-11-10_"))
            self.assertTrue(markdown_path.name.endswith(".complete.md"))
            markdown = markdown_path.read_text(encoding="utf-8")
            self.assertIn("Antigravity plan", markdown)
            self.assertIn("## System", markdown)
            self.assertIn("Workspace file:///workspace/project", markdown)
            self.assertIn("please continue", markdown)
            self.assertIn("need to inspect files", markdown)
            self.assertIn("file contents", markdown)
            self.assertNotIn("regular title", markdown)

            memory_path = run_dir / "memory.jsonl"
            corpus_path = run_dir / "corpus.jsonl"
            self.assertTrue(memory_path.is_file())
            self.assertTrue(corpus_path.is_file())
            memory_lines = memory_path.read_text(encoding="utf-8").splitlines()
            corpus_lines = corpus_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual((len(memory_lines), len(corpus_lines)), (1, 1))
            memory = json.loads(memory_lines[0])
            corpus = json.loads(corpus_lines[0])

            self.assertEqual(memory["record_type"], "session")
            self.assertEqual(memory["schema_version"], 2)
            self.assertEqual(memory["surface"], "standard")
            self.assertEqual(memory["date"], "2026-11-10")
            self.assertEqual(memory["workspace_uri"], "file:///workspace/project")
            self.assertEqual([message["role"] for message in memory["messages"]], ["assistant", "user"])
            self.assertEqual(memory["system_messages"][0]["text"],
                             "Workspace file:///workspace/project is ready")
            self.assertEqual(memory["reasoning"][0]["text"], "need to inspect files")
            self.assertEqual([call["name"] for call in memory["tool_calls"]], ["read_file", "wait"])
            self.assertEqual([item["result"] for item in memory["tool_results"]],
                             ["file contents", "wait complete"])
            self.assertTrue(all(item["linked_by"] == "order" for item in memory["tool_results"]))
            self.assertEqual(memory["generic_records"], [])

            self.assertEqual(corpus["record_type"], "corpus_document")
            self.assertEqual(corpus["id"], f"standard:{SID}")
            self.assertIn("please continue", corpus["text"])
            self.assertIn("need to inspect files", corpus["text"])
            self.assertIn("wait complete", corpus["text"])
            self.assertEqual(corpus["char_count"], len(corpus["text"]))

            metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["preset"], "complete")
            self.assertEqual(metadata["products"], ["markdown", "memory", "corpus"])
            self.assertEqual(metadata["sessions"][0]["output_files"], [
                markdown_path.relative_to(run_dir).as_posix(), "memory.jsonl", "corpus.jsonl",
            ])

    def test_transcript_without_dialogue_still_exports_system_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            logs = self._brain(home) / SID / ".system_generated" / "logs"
            logs.mkdir(parents=True)
            (logs / "transcript.jsonl").write_text(
                json.dumps({"step_index": 0, "source": "SYSTEM", "type": "SYSTEM_MESSAGE",
                            "status": "DONE", "created_at": "2026-11-14T19:12:20Z",
                            "content": "system only"}) + "\n",
                encoding="utf-8",
            )
            content = ContentOptions.from_values(["system"])

            result = export_all(
                home / "out", home=home, content=content,
                products=("memory",), preset="system",
            )

            self.assertEqual((result["exported"], result["failed"]), (1, 0))
            corpus = next((home / "out").glob("*/memory.jsonl"))
            record = json.loads(corpus.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual([item["text"] for item in record["system_messages"]], ["system only"])
            self.assertNotIn("messages", record)


if __name__ == "__main__":
    unittest.main()
