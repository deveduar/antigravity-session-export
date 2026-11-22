from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from version import __version__
from content import CONTENT_CHOICES, DEFAULT_PRODUCTS, PRODUCT_CHOICES, ContentOptions
from exporter import DEFAULT_PRESET, PRESET_CHOICES, SURFACE_CHOICES, export_all, list_sessions
from selection import SelectionOptions


ORDERS = ("updated-asc", "updated-desc", "id-asc", "id-desc")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export Antigravity conversations to Markdown, memory JSONL, or corpus JSONL run directories")
    parser.add_argument("--version", action="store_true", help="Print exporter version")
    parser.add_argument("--output", type=Path, default=Path("exports"),
                        help="Provider output directory that receives timestamped run directories (default: exports)")
    parser.add_argument("--antigravity-dir", type=Path, help="Override one brain directory")
    parser.add_argument("--surface", choices=SURFACE_CHOICES, default="auto",
                        help="Surface for --antigravity-dir; inferred from its parent path by default")
    parser.add_argument("--run-label", default="conversation", help="Label used in the run directory name")
    parser.add_argument("--preset", choices=PRESET_CHOICES, default=DEFAULT_PRESET,
                        help="Content preset used in run metadata and Markdown suffixes (default: conversation)")
    parser.add_argument("--last", type=int, help="Keep only the newest N sessions after sorting")
    parser.add_argument("--since", default="", help="Keep sessions updated at or after this ISO timestamp")
    parser.add_argument("--sessions", action="append", default=[],
                        help="Session id or surface:id; repeat or comma-separate")
    parser.add_argument("--filter", dest="filter_text", default="",
                        help="Case-insensitive text filter over title, id, surface and project")
    parser.add_argument("--order", choices=ORDERS, default="updated-desc", help="Session ordering")
    parser.add_argument("--product", dest="products", choices=PRODUCT_CHOICES, action="append",
                        default=None, help="Output product; repeat to combine (default: markdown)")
    parser.add_argument("--content", choices=CONTENT_CHOICES, action="append", default=None,
                         help="Content to include; repeat to combine (default: prompts answers reasoning)")

    parser.add_argument("--include-raw-reasoning", action="store_true",
                        help="Include raw thinking when reasoning is selected")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--list-sessions", action="store_true", help="List sessions without writing files")
    mode.add_argument("--check", action="store_true", help="Check session availability without writing files")
    parser.add_argument("--json", action="store_true", help="Emit the listing envelope as JSON")
    parser.add_argument("--dry-run", action="store_true", help="Resolve sessions without writing files")
    parser.add_argument("--antigravity-autostart", action="store_true",
                        help="Start Antigravity only if an offline .pb export needs live fallback")
    return parser


def _session_values(values: list[str]) -> tuple[str, ...]:
    items: list[str] = []
    for value in values:
        items.extend(part.strip() for part in value.split(",") if part.strip())
    return tuple(items)


def _selection(args: argparse.Namespace) -> SelectionOptions:
    if args.last is not None and args.last < 0:
        raise ValueError("--last must be zero or greater")
    return SelectionOptions(
        last=args.last,
        since=args.since,
        sessions=_session_values(args.sessions),
        filter_text=args.filter_text,
        order=args.order,
    )


def _products(args: argparse.Namespace) -> tuple[str, ...]:
    return tuple(dict.fromkeys(args.products or DEFAULT_PRODUCTS))


def _envelope(sessions: list[dict[str, Any]], selection: SelectionOptions) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "tool": "antigravity_export",
        "status": "ready" if sessions else "no_data",
        "writes": False,
        "sessions": sessions,
        "no_data_reason": "" if sessions else "no Antigravity sessions matched the selection",
        "filters": selection.as_dict(),
        "filters_key": selection.key(),
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.version:
        print(f"antigravity-session-export {__version__}")
        return 0
    try:
        selection = _selection(args)
    except ValueError as exc:
        print(f"[antigravity] {exc}", file=sys.stderr)
        return 2
    surface = None if args.surface == "auto" else args.surface
    if surface and not args.antigravity_dir:
        print("[antigravity] --surface requires --antigravity-dir", file=sys.stderr)
        return 2
    content = ContentOptions.from_values(args.content, args.include_raw_reasoning)

    if args.list_sessions or args.check:
        sessions = list_sessions(
            brain_override=args.antigravity_dir,
            surface=surface,
            selection=selection,
        )
        if args.json:
            print(json.dumps(_envelope(sessions, selection), ensure_ascii=False))
        else:
            for session in sessions:
                print(f"{session['id']}\t{session['title']}\t{session['updated']}")
        return 0 if sessions else 3

    result = export_all(
        args.output,
        brain_override=args.antigravity_dir,
        surface=surface,
        run_label=args.run_label,
        preset=args.preset,
        selection=selection,
        dry_run=args.dry_run,
        autostart=args.antigravity_autostart,
        content=content,
        products=_products(args),
    )
    suffix = " (dry-run)" if args.dry_run else ""
    print(
        f"[antigravity] run={result['run_id']} exported={result['exported']} "
        f"scanned={result['scanned']} failed={result['failed']}{suffix}"
    )
    for warning in result["warnings"]:
        print(
            f"[antigravity:{warning['surface']}] {warning['session_id']}: {warning['error']}",
            file=sys.stderr,
        )
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
