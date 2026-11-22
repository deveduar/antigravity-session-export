from __future__ import annotations

import base64
import builtins
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from cli import main
from content import CONTENT_CHOICES, ContentOptions
from exporter import SessionSource, export_all
from legacy_pb import (
    LEGACY_KEY,
    decrypt_legacy_pb,
    read_legacy_pb,
    read_legacy_pb_blocks,
)

OPAQUE_SIGNATURE = "SYNTHETIC_OPAQUE_SIGNATURE_DO_NOT_EXPORT"


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


def _timestamp(seconds: int) -> bytes:
    payload = _varint(8) + _varint(seconds)
    return _bytes_field(5, _bytes_field(1, payload))


def _legacy_step(step_type: int, text: str, seconds: int) -> bytes:
    nested = 19 if step_type == 14 else 20
    text_field = 2 if step_type == 14 else 1
    payload = _varint(8) + _varint(step_type)
    payload += _bytes_field(nested, _text_field(text_field, text))
    return payload + _timestamp(seconds)


def _trajectory(session_id: str) -> bytes:
    workspace = "file:///workspace/project"
    metadata = _text_field(7, workspace) + _bytes_field(1, _text_field(1, workspace))
    payload = _text_field(1, "traj-1")
    payload += _bytes_field(2, _legacy_step(14, "hola mundo", 1_700_000_000))
    payload += _bytes_field(2, _legacy_step(15, "respuesta", 1_700_000_001))
    payload += _text_field(6, session_id)
    payload += _bytes_field(7, metadata)
    return payload


def _tool_call(call_id: str, name: str, arguments: str, result: str) -> bytes:
    call = _text_field(1, call_id)
    call += _text_field(2, name)
    call += _text_field(3, arguments)
    call += _bytes_field(7, _text_field(2, result))
    return call


def _encrypted_tool_call(magic: str = "01be3ef6fb", as_base64: bool = False) -> bytes:
    call = _text_field(1, "call-encrypted")
    call += _text_field(2, "run_command")
    call += _text_field(3, "{}")
    inner = bytes.fromhex(magic) + bytes([0, 255, 1]) + b"secret-looking bytes"
    raw_result = b"\x0a" + _varint(len(inner)) + inner
    if as_base64:
        raw_result = base64.b64encode(b"\x12" + _varint(len(raw_result)) + raw_result)
    call += _bytes_field(7, _bytes_field(2, raw_result))
    return call


def _opaque_tool_call(as_base64: bool = False) -> bytes:
    reference = "11111111-2222-4333-8444-555555555555"
    result = f"${reference}" if not as_base64 else reference
    if as_base64:
        wrapper = b"\x12" + _varint(len(result) + 2) + b"\x0a" + _varint(len(result)) + result.encode()
        result = base64.b64encode(wrapper).decode("ascii")
    call = _text_field(1, "call-opaque")
    call += _text_field(2, "command_status")
    call += _text_field(3, "{}")
    call += _bytes_field(7, _text_field(2, result))
    return call


def _planner_step(
    answer: str,
    seconds: int,
    thinking: str = "",
    raw_thinking: str = "",
    signature: str = "",
    tool_calls: tuple[bytes, ...] = (),
) -> bytes:
    planner = _text_field(1, answer)
    if thinking:
        planner += _text_field(3, thinking)
    if raw_thinking:
        planner += _text_field(16, raw_thinking)
    if signature:
        planner += _text_field(14, signature)
    for call in tool_calls:
        planner += _bytes_field(7, call)
    payload = _varint(8) + _varint(15)
    payload += _bytes_field(20, planner)
    return payload + _timestamp(seconds)


def _loose_tool_step(text: str, seconds: int) -> bytes:
    payload = _varint(8) + _varint(21)
    payload += _text_field(10, text)
    return payload + _timestamp(seconds)


def _linked_tool_step(call_id: str, name: str, arguments: str, result: str, seconds: int) -> bytes:
    metadata = _text_field(1, call_id)
    metadata += _text_field(2, name)
    metadata += _text_field(3, arguments)
    metadata += _text_field(6, result)
    payload = _varint(8) + _varint(9)
    payload += _bytes_field(5, _bytes_field(4, metadata))
    return payload + _timestamp(seconds)


def _trajectory_with_content(session_id: str) -> bytes:
    workspace = "file:///workspace/project"
    metadata = _text_field(7, workspace) + _bytes_field(1, _text_field(1, workspace))
    call = _tool_call("call-1", "run_command", '{"command":"pwd"}', "linked tool output")
    planner = _planner_step(
        "final answer",
        1_700_000_001,
        thinking="visible thinking\u2028continued",
        raw_thinking="raw chain",
        signature=OPAQUE_SIGNATURE,
        tool_calls=(call,),
    )
    payload = _text_field(1, "traj-1")
    payload += _bytes_field(2, _legacy_step(14, "visible prompt", 1_700_000_000))
    payload += _bytes_field(2, planner)
    payload += _bytes_field(2, _loose_tool_step("loose step output", 1_700_000_002))
    payload += _text_field(6, session_id)
    payload += _bytes_field(7, metadata)
    return payload


def _trajectory_with_encrypted_result(
    session_id: str,
    magic: str = "01be3ef6fb",
    as_base64: bool = False,
) -> bytes:
    workspace = "file:///workspace/project"
    metadata = _text_field(7, workspace) + _bytes_field(1, _text_field(1, workspace))
    payload = _text_field(1, "traj-1")
    payload += _bytes_field(2, _legacy_step(14, "visible prompt", 1_700_000_000))
    payload += _bytes_field(2, _planner_step(
        "final answer",
        1_700_000_001,
        tool_calls=(_encrypted_tool_call(magic, as_base64),),
    ))
    payload += _text_field(6, session_id)
    payload += _bytes_field(7, metadata)
    return payload


def _trajectory_with_encrypted_tool_step(
    session_id: str,
    magic: str = "0172c8da7c",
) -> bytes:
    inner = bytes.fromhex(magic) + bytes([0, 255, 1]) + b"secret-looking bytes"
    raw = b"\x0a" + _varint(len(inner)) + inner
    text = base64.b64encode(b"\x12" + _varint(len(raw)) + raw).decode("ascii")
    workspace = "file:///workspace/project"
    metadata = _text_field(7, workspace) + _bytes_field(1, _text_field(1, workspace))
    payload = _text_field(1, "traj-1")
    payload += _bytes_field(2, _loose_tool_step(text, 1_700_000_002))
    payload += _text_field(6, session_id)
    payload += _bytes_field(7, metadata)
    return payload


def _trajectory_with_opaque_result(session_id: str, as_base64: bool = False) -> bytes:
    workspace = "file:///workspace/project"
    metadata = _text_field(7, workspace) + _bytes_field(1, _text_field(1, workspace))
    payload = _text_field(1, "traj-1")
    payload += _bytes_field(2, _legacy_step(14, "visible prompt", 1_700_000_000))
    payload += _bytes_field(2, _planner_step(
        "final answer",
        1_700_000_001,
        tool_calls=(_opaque_tool_call(as_base64),),
    ))
    payload += _text_field(6, session_id)
    payload += _bytes_field(7, metadata)
    return payload


def _trajectory_with_linked_encrypted_result(session_id: str) -> bytes:
    workspace = "file:///workspace/project"
    metadata = _text_field(7, workspace) + _bytes_field(1, _text_field(1, workspace))
    payload = _text_field(1, "traj-1")
    payload += _bytes_field(2, _legacy_step(14, "visible prompt", 1_700_000_000))
    payload += _bytes_field(2, _planner_step(
        "final answer",
        1_700_000_001,
        tool_calls=(_encrypted_tool_call(),),
    ))
    payload += _bytes_field(2, _linked_tool_step(
        "call-encrypted", "run_command", '{"command":"pwd"}', "plaintext tool result", 1_700_000_002,
    ))
    payload += _text_field(6, session_id)
    payload += _bytes_field(7, metadata)
    return payload


def _write_pb(path: Path, session_id: str = "legacy-session") -> bytes:
    plain = _trajectory(session_id)
    nonce = b"0123456789ab"
    path.write_bytes(nonce + AESGCM(LEGACY_KEY).encrypt(nonce, plain, None))
    return plain


def _write_content_pb(path: Path, session_id: str = "legacy-session") -> bytes:
    plain = _trajectory_with_content(session_id)
    nonce = b"0123456789ab"
    path.write_bytes(nonce + AESGCM(LEGACY_KEY).encrypt(nonce, plain, None))
    return plain


class LegacyPbTests(unittest.TestCase):
    def _export_content(
        self,
        source_path: Path,
        output_name: str,
        values: list[str],
        include_raw: bool = False,
        products: tuple[str, ...] | None = None,
        preset: str = "conversation",
    ) -> tuple[dict[str, object], str]:
        source = SessionSource("legacy-session", "ide", source_path)
        with patch("exporter.discover_sessions", return_value=[source]):
            result = export_all(
                source_path.parent / output_name,
                preset=preset,
                content=ContentOptions.from_values(values, include_raw),
                products=products,
            )
        suffix = ".jsonl" if products and products[0] in {"memory", "corpus"} else ".md"
        text = next((source_path.parent / output_name).rglob(f"*{suffix}")).read_text(encoding="utf-8")
        return result, text

    def test_decrypts_and_parses_offline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.pb"
            plain = _write_pb(path)
            self.assertEqual(decrypt_legacy_pb(path.read_bytes()), plain)
            convo = read_legacy_pb(path)
            self.assertEqual(convo.session_id, "legacy-session")
            self.assertEqual(convo.trajectory_id, "traj-1")
            self.assertEqual(convo.workspace_uri, "file:///workspace/project")
            self.assertEqual(len(convo.turns), 2)
            self.assertEqual([(turn.role, turn.text) for turn in convo.turns],
                             [("user", "hola mundo"), ("assistant", "respuesta")])
            self.assertEqual(convo.step_count, 2)

    def test_rejects_tampered_ciphertext(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.pb"
            _write_pb(path)
            raw = bytearray(path.read_bytes())
            raw[-1] ^= 0xFF
            path.write_bytes(raw)
            with self.assertRaisesRegex(ValueError, "authentication failed"):
                read_legacy_pb(path)

    def test_rejects_truncated_payload(self) -> None:
        with self.assertRaisesRegex(ValueError, "truncated"):
            decrypt_legacy_pb(b"short")

    def test_export_prefers_offline_pb_without_live_server(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path = root / "session.pb"
            _write_pb(source_path)
            source = SessionSource("legacy-session", "ide", source_path)
            with patch("exporter.discover_sessions",
                       return_value=[source]), patch(
                       "exporter.fetch_trajectory",
                       side_effect=AssertionError("live RPC must not be used")):
                result = export_all(root / "out")
            self.assertEqual(result["exported"], 1)
            self.assertEqual(result["failed"], 0)
            outputs = list((root / "out").rglob("*.md"))
            self.assertEqual(len(outputs), 1)
            self.assertIn("hola mundo", outputs[0].read_text(encoding="utf-8"))

    def test_missing_optional_dependency_uses_live_fallback(self) -> None:
        real_import = builtins.__import__

        def missing_crypto(name, *args, **kwargs):
            if name == "cryptography" or name.startswith("cryptography."):
                raise ImportError("No module named 'cryptography'")
            return real_import(name, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path = root / "session.pb"
            source_path.write_bytes(b"opaque legacy PB")
            source = SessionSource("legacy-session", "ide", source_path)
            fake_steps = [
                {"type": "CORTEX_STEP_TYPE_USER_INPUT", "userInput": {"userResponse": "Live question"}},
                {"type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE", "plannerResponse": {"response": "Live answer"}},
            ]
            with patch("builtins.__import__", side_effect=missing_crypto):
                with patch("exporter.discover_sessions",
                           return_value=[source]), patch(
                           "exporter.fetch_trajectory",
                           return_value=({"trajectoryId": "trajectory-1"}, fake_steps)):
                    result = export_all(root / "out")
            self.assertEqual(result["exported"], 1)
            self.assertEqual(result["failed"], 0)
            markdown = next((root / "out").rglob("*.md")).read_text(encoding="utf-8")
            self.assertIn("Live question", markdown)
            self.assertIn("Live answer", markdown)

    def test_read_legacy_pb_blocks_extracts_reasoning_and_linked_tool_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "content.pb"
            _write_content_pb(path)
            steps, reasoning, tool_calls = read_legacy_pb_blocks(path)
            conversation = read_legacy_pb(path)

            self.assertEqual(len(steps), 3)
            self.assertEqual(len(reasoning), 1)
            self.assertEqual(reasoning[0].text, "visible thinking\u2028continued")
            self.assertEqual(reasoning[0].raw_text, "raw chain")
            self.assertEqual(reasoning[0].signature, OPAQUE_SIGNATURE)
            self.assertEqual(len(tool_calls), 1)
            self.assertEqual(
                (tool_calls[0].id, tool_calls[0].name, tool_calls[0].arguments, tool_calls[0].result),
                ("call-1", "run_command", '{"command":"pwd"}', "linked tool output"),
            )
            self.assertEqual(conversation.reasoning, reasoning)
            self.assertEqual(conversation.tool_calls, tool_calls)

    def test_default_export_keeps_reasoning_and_tools_out_of_dialogue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "content.pb"
            _write_content_pb(path)
            _, markdown = self._export_content(path, "out-default", ["prompts", "answers"])

            self.assertIn("## User", markdown)
            self.assertIn("## Assistant", markdown)
            self.assertIn("visible prompt", markdown)
            self.assertIn("final answer", markdown)
            self.assertNotIn("visible thinking", markdown)
            self.assertNotIn("### Arguments", markdown)
            self.assertNotIn("linked tool output", markdown)

    def test_content_selection_renders_only_requested_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "content.pb"
            _write_content_pb(path)
            _, reasoning = self._export_content(path, "out-reasoning", ["reasoning"])
            _, calls = self._export_content(path, "out-calls", ["tool-calls"])
            _, results = self._export_content(path, "out-results", ["tool-results"])

            self.assertIn("## Reasoning", reasoning)
            self.assertIn("visible thinking", reasoning)
            self.assertNotIn("### Signature", reasoning)
            self.assertNotIn("## User", reasoning)
            self.assertNotIn("## Assistant", reasoning)
            self.assertNotIn("raw chain", reasoning)

            self.assertIn("## Tool call", calls)
            self.assertIn("Name: run_command", calls)
            self.assertIn("### Arguments", calls)
            self.assertIn('{"command":"pwd"}', calls)
            self.assertNotIn("### Result", calls)

            self.assertIn("## Tool result", results)
            self.assertIn("linked tool output", results)
            self.assertNotIn("loose step output", results)
            self.assertNotIn("## User", results)

    def test_opaque_signature_is_omitted_from_all_products(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "content.pb"
            plain = _write_content_pb(path)
            self.assertIn(OPAQUE_SIGNATURE.encode("ascii"), plain)

            _, markdown = self._export_content(path, "out-md", ["reasoning"])
            self.assertIn("visible thinking", markdown)
            self.assertNotIn(OPAQUE_SIGNATURE, markdown)
            self.assertNotIn("### Signature", markdown)

            for product in ("memory", "corpus"):
                _, text = self._export_content(
                    path, f"out-{product}", ["reasoning"], products=(product,)
                )
                self.assertIn("visible thinking", text)
                self.assertNotIn(OPAQUE_SIGNATURE, text)
                self.assertNotIn('"signature"', text)

    def test_include_raw_reasoning_is_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "content.pb"
            _write_content_pb(path)
            _, plain = self._export_content(path, "out-plain", ["reasoning"])
            _, raw = self._export_content(path, "out-raw", [], include_raw=True)

            self.assertNotIn("### Raw thinking", plain)
            self.assertIn("### Raw thinking", raw)
            self.assertIn("raw chain", raw)

    def test_memory_export_includes_all_selected_record_types(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "content.pb"
            _write_content_pb(path)
            values = ["prompts", "answers", "reasoning", "tool-calls", "tool-results"]
            _, text = self._export_content(path, "out-memory", values, products=("memory",))
            records = [json.loads(line) for line in text.splitlines()]

            self.assertEqual(len(records), 1)
            record = records[0]
            self.assertEqual(record["record_type"], "session")
            self.assertEqual(record["schema_version"], 2)
            self.assertEqual([message["role"] for message in record["messages"]],
                             ["user", "assistant"])
            self.assertEqual(record["reasoning"][0]["text"], "visible thinking\u2028continued")
            self.assertNotIn("signature", record["reasoning"][0])
            self.assertNotIn("raw_text", record["reasoning"][0])
            self.assertEqual(record["tool_calls"][0]["name"], "run_command")
            self.assertEqual(record["tool_results"][0]["result"], "linked tool output")

    def test_markdown_and_memory_products_write_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "content.pb"
            _write_content_pb(path)
            source = SessionSource("legacy-session", "ide", path)
            values = ["prompts", "answers", "reasoning", "tool-calls", "tool-results"]
            with patch("exporter.discover_sessions", return_value=[source]):
                result = export_all(
                    root / "out",
                    content=ContentOptions.from_values(values),
                    products=("markdown", "memory"),
                )
            run_dir = root / "out" / str(result["run_id"])
            metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))

            self.assertEqual(len(list(run_dir.rglob("*.md"))), 1)
            self.assertEqual([path.name for path in run_dir.glob("*.jsonl")], ["memory.jsonl"])
            self.assertEqual(metadata["products"], ["markdown", "memory"])
            self.assertTrue(metadata["sessions"][0]["output_file"].endswith(".conversation.md"))
            self.assertEqual(metadata["sessions"][0]["output_files"], [
                next(run_dir.rglob("*.md")).relative_to(run_dir).as_posix(), "memory.jsonl",
            ])

    def test_complete_preset_writes_suffixed_markdown_and_shared_corpora(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "content.pb"
            _write_content_pb(path)
            source = SessionSource("legacy-session", "standard", path)
            values = ["prompts", "answers", "reasoning", "tool-calls", "tool-results"]
            with patch("exporter.discover_sessions", return_value=[source]):
                result = export_all(
                    root / "out",
                    preset="complete",
                    content=ContentOptions.from_values(values),
                    products=("markdown", "memory", "corpus"),
                )
            run_dir = root / "out" / str(result["run_id"])
            names = sorted(path.name for path in run_dir.rglob("*") if path.is_file())
            metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))

            self.assertTrue(any(name.endswith(".complete.md") for name in names))
            self.assertIn("memory.jsonl", names)
            self.assertIn("corpus.jsonl", names)
            self.assertEqual(metadata["preset"], "complete")
            self.assertEqual(metadata["products"], ["markdown", "memory", "corpus"])

    def test_markdown_matches_complete_markdown_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "content.pb"
            _write_content_pb(path)
            source = SessionSource("legacy-session", "standard", path)
            bodies: dict[str, str] = {}

            for preset, products in (
                ("conversation", ("markdown",)),
                ("complete", ("markdown", "memory", "corpus")),
            ):
                with patch("exporter.discover_sessions", return_value=[source]):
                    result = export_all(
                        root / f"out-{preset}",
                        preset=preset,
                        content=ContentOptions.from_values(CONTENT_CHOICES),
                        products=products,
                    )
                self.assertEqual(result["exported"], 1)
                bodies[preset] = next((root / f"out-{preset}").rglob("*.md")).read_text(encoding="utf-8")

            self.assertEqual(bodies["conversation"], bodies["complete"])

    def test_corpus_export_flattens_one_document_per_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "content.pb"
            _write_content_pb(path)
            values = ["prompts", "answers", "reasoning", "tool-calls", "tool-results"]
            _, text = self._export_content(
                path, "out-corpus", values, products=("corpus",), preset="complete",
            )
            records = [json.loads(line) for line in text.splitlines()]

            self.assertEqual(len(records), 1)
            record = records[0]
            self.assertEqual(record["record_type"], "corpus_document")
            self.assertEqual(record["schema_version"], 2)
            self.assertEqual(record["id"], "ide:legacy-session")
            self.assertIn("User:", record["text"])
            self.assertIn("visible prompt", record["text"])
            self.assertIn("final answer", record["text"])
            self.assertIn("visible thinking", record["text"])
            self.assertIn("linked tool output", record["text"])
            self.assertEqual(record["char_count"], len(record["text"]))

    def test_cli_product_flag_writes_memory_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "content.pb"
            _write_content_pb(path)
            source = SessionSource("legacy-session", "ide", path)
            with patch("exporter.discover_sessions", return_value=[source]), \
                    contextlib.redirect_stdout(io.StringIO()):
                status = main([
                    "--output", str(root / "out"),
                    "--preset", "complete",
                    "--product", "memory",
                    "--content", "prompts",
                    "--content", "answers",
                    "--content", "reasoning",
                    "--content", "tool-calls",
                    "--content", "tool-results",
                ])

            jsonl = next((root / "out").glob("*/memory.jsonl"))
            records = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(status, 0)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["record_type"], "session")
            self.assertEqual([message["role"] for message in records[0]["messages"]],
                             ["user", "assistant"])

    def test_cli_content_flag_renders_selected_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "content.pb"
            _write_content_pb(path)
            source = SessionSource("legacy-session", "ide", path)
            with patch("exporter.discover_sessions", return_value=[source]), \
                    contextlib.redirect_stdout(io.StringIO()):
                status = main(["--output", str(root / "out"), "--content", "reasoning"])

            markdown = next((root / "out").rglob("*.md")).read_text(encoding="utf-8")
            self.assertEqual(status, 0)
            self.assertIn("visible thinking", markdown)
            self.assertNotIn("## Assistant", markdown)

    def test_default_cli_export_includes_reasoning_but_not_raw_or_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "content.pb"
            _write_content_pb(path)
            source = SessionSource("legacy-session", "ide", path)
            with patch("exporter.discover_sessions", return_value=[source]), \
                    contextlib.redirect_stdout(io.StringIO()):
                status = main(["--output", str(root / "out")])

            markdown = next((root / "out").rglob("*.md")).read_text(encoding="utf-8")
            self.assertEqual(status, 0)
            self.assertIn("visible thinking", markdown)
            self.assertNotIn("raw chain", markdown)
            self.assertNotIn("loose step output", markdown)

    def test_tool_steps_content_exports_loose_tool_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "content.pb"
            _write_content_pb(path)
            result, markdown = self._export_content(path, "out-toolsteps", ["tool-steps"])

            self.assertEqual(result["exported"], 1)
            self.assertIn("loose step output", markdown)
            self.assertNotIn("## User", markdown)

    def test_combined_selection_keeps_session_with_undecipherable_tool_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "encrypted.pb"
            plain = _trajectory_with_encrypted_result("legacy-session")
            nonce = b"0123456789ab"
            path.write_bytes(nonce + AESGCM(LEGACY_KEY).encrypt(nonce, plain, None))

            conversation = read_legacy_pb(path)
            self.assertTrue(conversation.tool_calls[0].result_encrypted)
            self.assertEqual(conversation.tool_calls[0].result, "")

            source = SessionSource("legacy-session", "ide", path)
            with patch("exporter.discover_sessions", return_value=[source]), patch(
                "exporter.fetch_trajectory",
                side_effect=AssertionError("live RPC must not be required"),
            ):
                result = export_all(
                    root / "out",
                    content=ContentOptions.from_values(["prompts", "answers", "tool-results"]),
                )

            self.assertEqual((result["exported"], result["failed"]), (1, 0))
            markdown = next((root / "out").rglob("*.md")).read_text(encoding="utf-8")
            self.assertIn("visible prompt", markdown)
            self.assertIn("final answer", markdown)
            self.assertNotIn("01be3ef6fb", markdown)
            self.assertEqual(result["warnings"], [])

    def test_markdown_selection_exports_session_with_encrypted_tool_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "encrypted.pb"
            plain = _trajectory_with_encrypted_result("legacy-session", "0172c8da7c", True)
            nonce = b"0123456789ab"
            path.write_bytes(nonce + AESGCM(LEGACY_KEY).encrypt(nonce, plain, None))

            conversation = read_legacy_pb(path)
            self.assertTrue(conversation.tool_calls[0].result_encrypted)
            self.assertEqual(conversation.tool_calls[0].result, "")

            source = SessionSource("legacy-session", "ide", path)
            with patch("exporter.discover_sessions", return_value=[source]), patch(
                "exporter.fetch_trajectory",
                side_effect=AssertionError("live RPC must not be required"),
            ):
                result = export_all(
                    root / "out",
                    content=ContentOptions.from_values(["prompts", "answers", "reasoning"]),
                )

            self.assertEqual((result["exported"], result["failed"]), (1, 0))
            markdown = next((root / "out").rglob("*.md")).read_text(encoding="utf-8")
            self.assertIn("visible prompt", markdown)
            self.assertIn("final answer", markdown)
            self.assertNotIn("secret-looking bytes", markdown)
            self.assertNotIn("0172c8da7c", markdown)

    def test_tool_calls_selection_exports_session_with_encrypted_tool_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "encrypted.pb"
            plain = _trajectory_with_encrypted_result("legacy-session", "01be3ef6fb", True)
            nonce = b"0123456789ab"
            path.write_bytes(nonce + AESGCM(LEGACY_KEY).encrypt(nonce, plain, None))

            source = SessionSource("legacy-session", "ide", path)
            with patch("exporter.discover_sessions", return_value=[source]), patch(
                "exporter.fetch_trajectory",
                side_effect=AssertionError("live RPC must not be required"),
            ):
                result = export_all(
                    root / "out",
                    content=ContentOptions.from_values(["tool-calls"]),
                )

            self.assertEqual((result["exported"], result["failed"]), (1, 0))
            markdown = next((root / "out").rglob("*.md")).read_text(encoding="utf-8")
            self.assertIn("## Tool call", markdown)
            self.assertNotIn("### Result", markdown)
            self.assertNotIn("secret-looking bytes", markdown)

    def test_base64_encrypted_tool_results_skip_both_magics(self) -> None:
        for magic in ("01be3ef6fb", "0172c8da7c"):
            with self.subTest(magic=magic), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "encrypted.pb"
                plain = _trajectory_with_encrypted_result("legacy-session", magic, True)
                nonce = b"0123456789ab"
                path.write_bytes(nonce + AESGCM(LEGACY_KEY).encrypt(nonce, plain, None))

                conversation = read_legacy_pb(path)
                self.assertTrue(conversation.tool_calls[0].result_encrypted)
                self.assertEqual(conversation.tool_calls[0].result, "")

                source = SessionSource("legacy-session", "ide", path)
                with patch("exporter.discover_sessions", return_value=[source]), patch(
                    "exporter.fetch_trajectory",
                    side_effect=AssertionError("live RPC must not be required"),
                ):
                    result = export_all(
                        root / "out",
                        content=ContentOptions.from_values(["tool-results"]),
                    )

                self.assertEqual((result["exported"], result["failed"]), (0, 1))
                self.assertEqual(result["run_id"], "")
                self.assertFalse((root / "out").exists())
                self.assertEqual(len(result["warnings"]), 1)
                self.assertIn("no exportable selected content", result["warnings"][0]["error"])

    def test_base64_encrypted_tool_step_is_not_exported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "encrypted-step.pb"
            plain = _trajectory_with_encrypted_tool_step("legacy-session")
            nonce = b"0123456789ab"
            path.write_bytes(nonce + AESGCM(LEGACY_KEY).encrypt(nonce, plain, None))

            source = SessionSource("legacy-session", "ide", path)
            with patch("exporter.discover_sessions", return_value=[source]), patch(
                "exporter.fetch_trajectory",
                side_effect=AssertionError("live RPC must not be required"),
            ):
                result = export_all(
                    root / "out",
                    content=ContentOptions.from_values(["tool-steps"]),
                )

            self.assertEqual((result["exported"], result["failed"]), (0, 1))
            self.assertEqual(list((root / "out").rglob("*.md")), [])

    def test_opaque_tool_results_skip_direct_and_base64(self) -> None:
        for as_base64 in (False, True):
            with self.subTest(as_base64=as_base64), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "opaque.pb"
                plain = _trajectory_with_opaque_result("legacy-session", as_base64)
                nonce = b"0123456789ab"
                path.write_bytes(nonce + AESGCM(LEGACY_KEY).encrypt(nonce, plain, None))

                conversation = read_legacy_pb(path)
                self.assertTrue(conversation.tool_calls[0].result_encrypted)
                self.assertEqual(conversation.tool_calls[0].result, "")

                source = SessionSource("legacy-session", "ide", path)
                with patch("exporter.discover_sessions", return_value=[source]), patch(
                    "exporter.fetch_trajectory",
                    side_effect=AssertionError("live RPC must not be required"),
                ):
                    result = export_all(
                        root / "out",
                        content=ContentOptions.from_values(["tool-results"]),
                    )

                self.assertEqual((result["exported"], result["failed"]), (0, 1))
                self.assertEqual(result["run_id"], "")
                self.assertFalse((root / "out").exists())
                self.assertEqual(len(result["warnings"]), 1)
                self.assertIn("no exportable selected content", result["warnings"][0]["error"])

    def test_markdown_selection_exports_session_with_opaque_tool_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "opaque.pb"
            plain = _trajectory_with_opaque_result("legacy-session", True)
            nonce = b"0123456789ab"
            path.write_bytes(nonce + AESGCM(LEGACY_KEY).encrypt(nonce, plain, None))

            source = SessionSource("legacy-session", "ide", path)
            with patch("exporter.discover_sessions", return_value=[source]), patch(
                "exporter.fetch_trajectory",
                side_effect=AssertionError("live RPC must not be required"),
            ):
                result = export_all(
                    root / "out",
                    content=ContentOptions.from_values(["prompts", "answers", "reasoning"]),
                )

            self.assertEqual((result["exported"], result["failed"]), (1, 0))
            markdown = next((root / "out").rglob("*.md")).read_text(encoding="utf-8")
            self.assertIn("visible prompt", markdown)
            self.assertIn("final answer", markdown)
            self.assertNotIn("$11111111-2222-4333-8444-555555555555", markdown)
            self.assertNotIn("EiYKJGUyNDgzMGE3", markdown)

    def test_tool_step_metadata_links_plaintext_result_by_call_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "linked.pb"
            plain = _trajectory_with_linked_encrypted_result("legacy-session")
            nonce = b"0123456789ab"
            path.write_bytes(nonce + AESGCM(LEGACY_KEY).encrypt(nonce, plain, None))

            conversation = read_legacy_pb(path)
            self.assertEqual(len(conversation.tool_calls), 1)
            call = conversation.tool_calls[0]
            self.assertEqual(call.result, "plaintext tool result")
            self.assertEqual(call.linked_by, "call_id")
            self.assertFalse(call.result_encrypted)
            self.assertEqual(conversation.tool_steps, ())

            source = SessionSource("legacy-session", "ide", path)
            with patch("exporter.discover_sessions", return_value=[source]), patch(
                "exporter.fetch_trajectory",
                side_effect=AssertionError("live RPC must not be required"),
            ):
                result = export_all(
                    root / "out",
                    content=ContentOptions.from_values(["tool-results", "tool-steps"]),
                )

            self.assertEqual((result["exported"], result["failed"]), (1, 0))
            markdown = next((root / "out").rglob("*.md")).read_text(encoding="utf-8")
            self.assertIn("plaintext tool result", markdown)
            self.assertNotIn("_No linked tool results found._", markdown)
            self.assertNotIn("01be3ef6fb", markdown)

    def test_session_without_selected_content_is_not_exported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "empty.pb"
            _write_pb(path)
            source = SessionSource("legacy-session", "ide", path)
            with patch("exporter.discover_sessions", return_value=[source]), patch(
                "exporter.fetch_trajectory",
                side_effect=AssertionError("live RPC must not be required"),
            ):
                result = export_all(
                    root / "out",
                    content=ContentOptions.from_values(["tool-results"]),
                )

            self.assertEqual((result["exported"], result["failed"]), (0, 1))
            self.assertEqual(result["run_id"], "")
            self.assertFalse((root / "out").exists())
            self.assertEqual(len(result["warnings"]), 1)
            self.assertIn("no exportable selected content", result["warnings"][0]["error"])


if __name__ == "__main__":
    unittest.main()
