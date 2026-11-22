# Antigravity Session Exporter

Local exporter for Google Antigravity chat sessions. It writes readable
Markdown, a structured memory JSONL corpus, and a flattened corpus JSONL file.

## Quick start

```bash
# Optional: enable offline decoding of legacy .pb files.
python3 -m pip install "cryptography>=41"

# Read-only inventory: preflight and session list.
python3 export_sessions.py --check --json
python3 export_sessions.py --list-sessions --json

# Export default content into a fresh timestamped run directory.
python3 export_sessions.py --output exports --run-label conversation

# Export all products from the same deduplicated session set.
python3 export_sessions.py --output exports --run-label complete --preset complete --product markdown --product memory --product corpus --content prompts --content answers --content reasoning --content system --content tool-calls --content tool-results --content tool-steps
```

The first install is optional and enables offline `.pb` decoding. Omit it if
`--check` and `--list-sessions` already show the sessions you need. Use `python`
instead of `python3` on Windows.

## Requirements and installation

- Python 3.10 or later.
- No required third-party runtime packages.
- `export_sessions.py` is the portable entry point; it runs from a checkout.
- `python3 -m pip install -e .` installs the `antigravity-session-export` command.
- `python3 -m pip install "cryptography>=41"` enables offline legacy `.pb` decoding.
- `python3 -m cli` from `src/` is an equivalent flat-module entry point.

The source modules live directly in `src/`.

## Sources and discovery

Source types:

- `conversations/<uuid>.db`: SQLite user-input and planner-response steps.
- `conversations/<uuid>.pb`: encrypted legacy trajectory.
- `brain/<uuid>/.system_generated/logs/transcript*.jsonl`: readable transcript.

Default scan roots:

- `~/.gemini/antigravity`: surface `standard`.
- `~/.gemini/antigravity-ide`: surface `ide`.
- `~/.gemini/antigravity-cli`: surface `cli`.
- Backups: `conversations_backup` in every root, plus `antigravity-backup`
  beside the `standard` root.
- `~/.config/Antigravity` is not scanned.

Precedence inside one surface:

1. Main store over backups.
2. `transcript_full.jsonl` over `transcript.jsonl`, `.db`, and `.pb`.

Surface selection:

- `--surface standard|ide|cli` narrows discovery to one surface.
- `legacy` and numeric surface aliases are not accepted.
- `--antigravity-dir <brain-dir>` replaces all roots with that brain's parent.
- The override root implies `ide`, `cli`, or `standard` from its directory name.
- `--surface` requires `--antigravity-dir`.

Deduplication happens at export time:

- Same UUID plus identical selected content: keep one session.
- Preference: `ide`, then `cli`, then `standard`.
- Same UUID with different content: keep both.
- `--check` and `--list-sessions` remain per-surface inventories.

Legacy `.pb` decoding:

- Uses the public AES-256-GCM key and a small protobuf reader matching
  [`jkfujinami/antigravity-grpc-schemas`](https://github.com/jkfujinami/antigravity-grpc-schemas).
- If `cryptography` is missing or decoding fails, the exporter reuses a running
  local language server over loopback RPC.
- The CSRF token stays in memory and is sent only to `127.0.0.1`.
- `--antigravity-autostart` may start and stop Antigravity only at that fallback.
- `ANTIGRAVITY_LAUNCHER` overrides executable discovery.

## Command cheat sheet

```bash
# Export default content into a fresh timestamped run directory.
python3 export_sessions.py --output <output-root> --run-label conversation

# Preview without writing products or metadata.
python3 export_sessions.py --dry-run --last 5

# List sessions as text or JSON.
python3 export_sessions.py --list-sessions
python3 export_sessions.py --list-sessions --json

# Read-only preflight with selection filters applied.
python3 export_sessions.py --check --json --since 2026-01-01T00:00:00Z

# Select exact sessions by bare UUID or surface:UUID.
python3 export_sessions.py --list-sessions --json --sessions standard:session-id-1,ide:session-id-2

# Combine a text filter with explicit ordering.
python3 export_sessions.py --list-sessions --json --filter workspace-name --order updated-desc

# Override one brain directory for discovery.
python3 export_sessions.py --check --json --antigravity-dir /path/to/brain

# Interpret that override as a specific surface.
python3 export_sessions.py --check --json --antigravity-dir /path/to/brain --surface ide

# Write every product from the same selected content.
python3 export_sessions.py --preset complete --product markdown --product memory --product corpus --content prompts --content answers --content reasoning --content system --content tool-calls --content tool-results --content tool-steps

# Emit a flattened one-line-per-session corpus.
python3 export_sessions.py --preset complete --product corpus --content prompts --content answers --content reasoning --content system --content tool-calls --content tool-results --content tool-steps

# Print the installed module version.
PYTHONPATH=src python3 -m cli --version
```

- `--output` defaults to `exports/`; it receives timestamped run directories.
- `--list-sessions` and `--check` are read-only; add `--json` for envelopes.
- `--dry-run` resolves and decodes plans but writes nothing.

Exit codes:

| Code | Meaning |
|---|---|
| `0` | Ready or successful export. |
| `1` | Export/runtime failure; a per-session failure makes the run fail. |
| `2` | Invalid usage. |
| `3` | No data or no session matched the selection. |

## Selection flags

All selection flags compose together and apply before export, listing, and
check.

- `--last N`: keep the newest N sessions after sorting.
- `--since ISO`: keep sessions updated at or after this UTC timestamp.
- `--sessions ID`: repeat or comma-separate UUIDs or `standard|ide|cli:UUID` ids.
- `--surface VALUE`: `standard`, `ide`, or `cli`; requires `--antigravity-dir`.
- `--filter TEXT`: case-insensitive match over title, id, surface, project, and
  source path.
- `--order`: `updated-asc`, `updated-desc` (default), `id-asc`, or `id-desc`.

## Content selection

`--content` chooses what products contain. Repeat it to combine values. Omitting
it selects `prompts`, `answers`, and `reasoning`.

| Value | Markdown section |
|---|---|
| `prompts` | User messages (`## User — …`). |
| `answers` | Assistant responses (`## Assistant — …`). |
| `reasoning` | Planner thinking (`## Reasoning — …`). |
| `system` | Transcript `SYSTEM_MESSAGE` records (`## System — …`). |
| `tool-calls` | Tool id, name, and arguments (`## Tool call — …`). |
| `tool-results` | Decrypted results linked to a `ChatToolCall`. |
| `tool-steps` | Unlinked raw tool step payloads (`## Tool step — …`). |

Reasoning and signatures:

- `--include-raw-reasoning` adds `### Raw thinking` when the source has it.
- It implies `reasoning`.
- Signatures are opaque provider state and are never written to any product.
- Readable thinking is exported even when signatures are omitted.

Tool result sources:

- Plaintext result on `ChatToolCall` (`f7 → f2 → f1`).
- Tool-step metadata matched by `call_id` (`f5 → f4 → f1`, result in `f6`).
- Flat-transcript `GENERIC` steps linked by order to the preceding
  `PLANNER_RESPONSE`.

Tool result rules:

- Linked results carry `linked_by: "call_id"` or `"order"`.
- Linked results are not duplicated as raw tool steps or generic records.
- Unlinked steps stay under `tool-steps` or `tool-results`.
- Encrypted magics, `$uuid` references, and base64-wrapped UUIDs are
  undecipherable and never emitted.
- Undecipherable parts are dropped individually.
- A combined selection keeps readable dialogue, reasoning, and tool records.
- A tool-only selection with nothing readable left is a failed session.
- Autostart may retry through the local language server before dropping a part.
- Empty sections and placeholder text are never emitted.

```bash
# Dialogue plus readable reasoning.
python3 export_sessions.py --content prompts --content answers --content reasoning

# Reasoning only, including raw thinking when the source has it.
python3 export_sessions.py --content reasoning --include-raw-reasoning

# Only results attached to a ChatToolCall.
python3 export_sessions.py --content tool-results

# Only raw tool step payloads.
python3 export_sessions.py --content tool-steps
```

`--content` and `--include-raw-reasoning` do not affect discovery, `--check`,
`--list-sessions`, or session selection.

## Output products and presets

`--product` chooses files and may be repeated. `--preset` labels content and
filenames. Defaults are `markdown` and `conversation`.

Products:

- `markdown`: one `<date>_<title>_<surface>.<preset>.md` per session inside a
  project group directory; contains front matter and selected sections.
- `memory`: `memory.jsonl`; one `record_type: "session"` object per session with
  selected `messages`, `system_messages`, `reasoning`, `tool_calls`,
  `tool_results`, `tool_steps`, and `generic_records`.
- `corpus`: `corpus.jsonl`; one `record_type: "corpus_document"` per session with
  flattened `text`, `char_count`, metadata, and selected `content` names.

Presets:

- Content presets: `conversation`, `prompts`, `markdown`, `reasoning`, `system`,
  `tool-calls`, `tool-results`, and `tool-steps`.
- `complete` is a preset, not a product selector.
- `complete` does not create files by itself; combine it with `--product`.
- All products use the same deduplicated session set.

```bash
# Every selected content type as one memory corpus line per session.
python3 export_sessions.py --preset complete --product memory --content prompts --content answers --content reasoning --content system --content tool-calls --content tool-results --content tool-steps

# Markdown plus both shared corpora from the same run.
python3 export_sessions.py --preset complete --product markdown --product memory --product corpus --content prompts --content answers --content reasoning --content system --content tool-calls --content tool-results --content tool-steps
```

Naming and fields:

- Dates are UTC source modification times, not run time.
- Group date is the newest exported session date for that project.
- Group name uses the decoded workspace's last path component plus a stable URI
  hash, or a single `no-project` component.
- Markdown filenames start with session date, then title, then surface.
- Corpus files have no date in their names or a date header; dates stay in JSON.
- Corpus lines share `schema_version`, `tool`, `surface`, `session_id`,
  `trajectory_id`, `title`, `date`, `modified_at`, `workspace_uri`,
  `source_path`, and `step_count`.
- `raw_text` appears in memory reasoning items only with
  `--include-raw-reasoning`.

## Output and read-only contracts

Each export with at least one exported session creates a fresh run directory:

```text
<output>/YYYYMMDDTHHMMSSZ_<label>/
├── metadata.json
├── 2026-11-10_9f3c2a1b4d5e/
│   └── 12345678_standard.complete.md
├── memory.jsonl
└── corpus.jsonl
```

Run rules:

- Markdown files are per session; corpus files are shared by the run.
- Same-second run-name collisions append `_2`, `_3`, and so on.
- Markdown headings use UTC ISO 8601 timestamps, for example
  `## User — 2026-09-14T23:48:51Z`.

Failure rules:

- If all selected sessions fail, `exported` is `0`, `failed` is `N`,
  `run_id` is `""`, and `run_dir` is `""`.
- No output root, run directory, Markdown, memory, corpus, or metadata is
  written for an all-failed export.
- Failures remain in `warnings` on stdout/stderr.

Metadata rules:

- `metadata.json` includes `schema_version`, `tool`, `version`, `run_id`,
  `created_at`, `run_label`, `preset`, `selection`, `selection_key`,
  per-surface counts, warnings, and per-session status, revision, source path,
  and output file.
- Non-default product or content selections add `products`, `content`, and,
  when used, `include_raw_reasoning`.
- Runs with multiple products add `output_files` per session.
- There is no state file and no compatibility path for old stable archives.

`--list-sessions --json` and `--check --json` emit:

```json
{
  "schema_version": 1,
  "tool": "antigravity_export",
  "status": "ready",
  "writes": false,
  "sessions": [
    {
      "id": "standard:session-id",
      "session_id": "session-id",
      "surface": "standard",
      "storage_format": "pb",
      "title": "Conversation title",
      "project_directory": "/workspace/project",
      "archived": false,
      "step_count": 12,
      "updated": "2026-09-14T23:48:51Z",
      "last_modified_time": "1757893731000000000",
      "revision": "opaque-revision",
      "has_transcript": false,
      "transcript_path": null,
      "conversation_path": "/home/user/.gemini/antigravity/conversations/session-id.pb",
      "source_path": "/home/user/.gemini/antigravity/conversations/session-id.pb"
    }
  ],
  "no_data_reason": "",
  "filters": {"last": null, "since": "", "sessions": [], "filter": "", "order": "updated-desc"},
  "filters_key": "opaque-filter-key"
}
```

`status` is `ready`, `no_data`, or `error`; `writes` is always `false`. Export
summaries go to stdout and per-session warnings to stderr. Read-only envelopes
use the annotation title when present, then recover it from local DB, transcript,
or offline `.pb` content. Offline `.pb` titles require the optional `cryptography`
install.

## Verification

```bash
# Run the exporter test suite.
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Roadmap

- [ ] Validate live RPC fallback on each supported host; tests cover offline
  `.pb` decoding and a mocked fallback.
- [ ] Validate `--antigravity-autostart` end to end. The exporter starts and
  stops only the process it launches; a launcher only forwards the option.
- [ ] Verify optional `cryptography` decoding with Windows `python` and
  POSIX/remote `python3`.
- [x] Add content-level selection for reasoning, system records, and tool steps.
- [x] Add memory and corpus products with one line per session.
- [x] Link flat-transcript `GENERIC` results to the preceding planner call.
- [x] Deduplicate identical sessions across surfaces before writing products.

## License & Attribution

gRPC/Protobuf schemas were reverse-engineered and extracted by [ふじゃ](https://github.com/jkfujinami/antigravity-grpc-schemas)

MIT; see [`LICENSE`](./LICENSE).
