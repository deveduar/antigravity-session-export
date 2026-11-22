# Changelog

All notable changes to this project are documented here. Entries are listed
newest first.

## [Unreleased]

### Changed

- Restored explanatory comments above README command examples and brought back
  the product and preset examples removed during an earlier README cleanup.
- Removed shell line continuations from all README command examples.
- Documented how read-only envelopes recover session titles and that offline
  `.pb` titles require the optional `cryptography` install.
- Replaced realistic opaque-signature and tool-reference test fixtures with
  obviously synthetic values.

## 2026-11-22 — f333af3

### Added

- Recovered session titles for `--list-sessions --json` from annotation titles,
  local SQLite conversations, JSONL transcripts, and offline legacy `.pb`
  content.
- Added first-non-empty-line title extraction for DB, transcript, live RPC, and
  legacy `.pb` sources.

### Changed

- Organized and clarified the exporter README.

## 2026-11-22 — 491d5e2

### Added

- Added canonical `--surface standard|ide|cli` discovery and removed `legacy`
  and numeric surface aliases.
- Added `--surface` auto-inference from the override directory name; explicit
  `--surface` without `--antigravity-dir` is invalid usage.
- Deduplication keeps one session per UUID+content signature, preferring `ide`
  over `cli` over `standard`; failed and differing-content sessions are retained.
- Offline DB/PB/transcript sources retry via live RPC when selected content is
  missing or undecipherable, preserving the offline source's date, title, and
  workspace.

### Fixed

- Fixed legacy detection and duplicate `no-project` folder labels.
- Filtered undecipherable tool parts individually; linked results are not
  duplicated and tool-only selections with nothing readable fail the session.
- An all-failed export returns empty `run_id`/`run_dir` and writes no output
  root, products, or metadata; failures stay in warnings.

## 2026-11-21 — 67e148f

### Added

- Added content-level selection for reasoning, system records, tool results,
  and tool steps.
- Expanded flat-transcript handling, linked generic tool results, tests, and
  documentation.

### Changed

- Changed the default `--output` root from the user data directory to `exports/`.
- Added reasoning to the default `--content` selection; raw thinking remains
  opt-in.

### Removed

- Removed provider signatures from all products; readable thinking is exported
  without them.

## 2026-11-20 — f885a4d

### Added

- Added the product model for Markdown, memory, and corpus outputs.
- Added preset labels for Markdown files and run metadata, and deduplicated
  identical source sessions before writing products.

### Changed

- Renamed output formats to products (`memory`, `corpus`) with fixed JSONL
  filenames; presets label content and filenames only and write nothing by
  themselves.

## 2026-11-20 — 96a87be

### Added

- Added memory and corpus products, content selection, and surface support.
- Added opt-in `--include-raw-reasoning` (implies `reasoning`) for
  `### Raw thinking` Markdown and `raw_text` memory fields.
- Moved source modules to a flat `src/` layout and added `src/version.py`.

## 2026-11-19 — bb5c810

### Added

- Added selection and filtering flags, detailed README documentation, a LICENSE,
  and the `antigravity-session-export` console entry point.
- Added documented exit codes: `0` ready/success, `1` export or per-session
  failure, `2` invalid usage, `3` no data/no match.
- Added read-only `--check`/`--list-sessions` envelopes that never write output,
  including `status`, `writes:false`, `no_data_reason`, and filter metadata.

### Changed

- Organized the package under `src/antigravity_session_export` and expanded the
  test suite.

### Removed

- Removed the persistent state file and skip-unchanged incremental exports;
  each run now creates a fresh timestamped run directory with `metadata.json`.

## 2026-11-19 — 6d5b09d

### Changed

- Moved source modules under `src/antigravity_session_export` and removed
  bytecode caches from version control.

## 2026-11-19 — a84bae2

### Added

- Initial functional release with offline legacy `.pb` decoding, loopback live
  RPC fallback, SQLite and transcript readers, export deduplication, and tests.
- Live fallback is loopback-only; `--antigravity-autostart` starts and stops only
  its own process, and missing `cryptography` falls back to live RPC or records
  a failure.
