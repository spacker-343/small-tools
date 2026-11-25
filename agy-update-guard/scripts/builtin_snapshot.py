#!/usr/bin/env python3
"""Snapshot and diff utility for local Antigravity builtin skills and rules."""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ALLOWED_EXTENSIONS = {".md", ".json", ".mjs", ".js", ".yml", ".yaml", ".txt"}
MAX_FILE_BYTES = 512 * 1024
DEFAULT_MAX_LINES_PER_FILE = 80
DEFAULT_MAX_TOTAL_CHARS = 6000


def get_default_builtin_dir() -> Path:
    """Return the default Antigravity builtin directory."""
    return Path.home() / ".gemini" / "antigravity" / "builtin"


def collect_snapshot(builtin_dir: Path | str | None = None) -> dict[str, Any]:
    """Scan builtin_dir and collect deterministic hashes and UTF-8 text contents."""
    root = Path(builtin_dir) if builtin_dir is not None else get_default_builtin_dir()
    files: dict[str, dict[str, Any]] = {}
    skipped: list[dict[str, str]] = []

    if root.exists() and root.is_dir():
        candidates = sorted(
            (p for p in root.rglob("*") if p.is_file()),
            key=lambda p: p.relative_to(root).as_posix(),
        )
        for path in candidates:
            rel_path = path.relative_to(root).as_posix()
            if path.suffix.lower() not in ALLOWED_EXTENSIONS:
                continue
            try:
                raw = path.read_bytes()
            except OSError as exc:
                skipped.append({"path": rel_path, "reason": f"read_error: {exc}"})
                continue
            if len(raw) > MAX_FILE_BYTES:
                skipped.append({"path": rel_path, "reason": "exceeds_512kb"})
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                skipped.append({"path": rel_path, "reason": "non_utf8_binary"})
                continue

            files[rel_path] = {
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
                "content": text,
            }

    return {
        "schema_version": 1,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "builtin_dir": str(root),
        "exists": root.exists() and root.is_dir(),
        "file_count": len(files),
        "files": files,
        "skipped": skipped,
    }


def load_snapshot(path: Path | str) -> dict[str, Any] | None:
    """Load a snapshot JSON file if it exists, otherwise return None."""
    target = Path(path)
    if not target.exists():
        return None
    return json.loads(target.read_text(encoding="utf-8"))


def save_snapshot(snapshot: dict[str, Any], path: Path | str) -> None:
    """Write a snapshot dictionary atomically to JSON."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_suffix(target.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(target)


def _format_file_unified_diff(
    rel_path: str,
    old_text: str,
    new_text: str,
    max_lines: int,
) -> str:
    """Build a bounded unified diff string for one modified file."""
    diff_lines = list(
        difflib.unified_diff(
            old_text.splitlines(),
            new_text.splitlines(),
            fromfile=f"baseline/{rel_path}",
            tofile=f"current/{rel_path}",
            lineterm="",
            n=2,
        )
    )
    if not diff_lines:
        return ""
    if len(diff_lines) > max_lines:
        omitted = len(diff_lines) - max_lines
        diff_lines = diff_lines[:max_lines] + [
            f"... [file diff truncated: {omitted} more lines omitted]"
        ]
    return "\n".join(diff_lines)


def diff_snapshots(
    baseline: dict[str, Any] | None,
    current: dict[str, Any],
    max_lines_per_file: int = DEFAULT_MAX_LINES_PER_FILE,
    max_total_chars: int = DEFAULT_MAX_TOTAL_CHARS,
) -> dict[str, Any]:
    """Compare baseline snapshot against current snapshot and return compact diffs."""
    base_files: dict[str, dict[str, Any]] = (
        baseline.get("files", {}) if isinstance(baseline, dict) else {}
    )
    curr_files: dict[str, dict[str, Any]] = (
        current.get("files", {}) if isinstance(current, dict) else {}
    )

    base_keys = set(base_files.keys())
    curr_keys = set(curr_files.keys())

    added = sorted(curr_keys - base_keys)
    removed = sorted(base_keys - curr_keys)
    modified = sorted(
        key
        for key in (base_keys & curr_keys)
        if base_files[key].get("sha256") != curr_files[key].get("sha256")
    )

    diffs: dict[str, str] = {}
    summary_chunks: list[str] = []
    total_chars = 0
    truncated = False

    for rel_path in modified:
        old_text = str(base_files[rel_path].get("content", ""))
        new_text = str(curr_files[rel_path].get("content", ""))
        file_diff = _format_file_unified_diff(
            rel_path, old_text, new_text, max_lines_per_file
        )
        if not file_diff:
            continue
        diffs[rel_path] = file_diff
        if not truncated:
            sep = "\n\n" if summary_chunks else ""
            candidate_len = total_chars + len(sep) + len(file_diff)
            if candidate_len <= max_total_chars:
                summary_chunks.append(file_diff)
                total_chars = candidate_len
            else:
                remaining = max_total_chars - total_chars - len(sep)
                if remaining > 40:
                    summary_chunks.append(file_diff[:remaining])
                summary_chunks.append(
                    f"\n... [total diff truncated at {max_total_chars} chars]"
                )
                truncated = True

    diff_summary = "\n\n".join(summary_chunks)
    if len(diff_summary) > max_total_chars + 80 and not truncated:
        diff_summary = (
            diff_summary[:max_total_chars]
            + f"\n... [total diff truncated at {max_total_chars} chars]"
        )
        truncated = True

    has_changes = bool(added or removed or modified)
    return {
        "has_changes": has_changes,
        "added": added,
        "removed": removed,
        "modified": modified,
        "diffs": diffs,
        "diff_summary": diff_summary,
        "truncated": truncated,
        "baseline_file_count": len(base_files),
        "current_file_count": len(curr_files),
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Snapshot and diff local Antigravity builtin files."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    snap_p = sub.add_parser("snapshot", help="Capture current builtin directory snapshot.")
    snap_p.add_argument(
        "--builtin-dir",
        default=None,
        help="Override builtin directory (default: ~/.gemini/antigravity/builtin).",
    )
    snap_p.add_argument(
        "--output",
        default=None,
        help="Output JSON path (omit or '-' to print to stdout).",
    )

    diff_p = sub.add_parser("diff", help="Diff current builtin directory against baseline JSON.")
    diff_p.add_argument(
        "--baseline",
        required=True,
        help="Path to baseline snapshot JSON.",
    )
    diff_p.add_argument(
        "--builtin-dir",
        default=None,
        help="Override builtin directory (default: ~/.gemini/antigravity/builtin).",
    )
    diff_p.add_argument(
        "--json",
        action="store_true",
        help="Emit structured diff result as JSON.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.command == "snapshot":
        snap = collect_snapshot(args.builtin_dir)
        if args.output and args.output != "-":
            save_snapshot(snap, args.output)
            print(f"Saved snapshot ({snap['file_count']} files) to {args.output}")
        else:
            print(json.dumps(snap, ensure_ascii=False, indent=2))
        return 0

    if args.command == "diff":
        baseline = load_snapshot(args.baseline)
        if baseline is None:
            print(f"Baseline file not found: {args.baseline}", file=sys.stderr)
            return 2
        current = collect_snapshot(args.builtin_dir)
        result = diff_snapshots(baseline, current)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"has_changes: {result['has_changes']}")
            print(f"added ({len(result['added'])}): {result['added']}")
            print(f"removed ({len(result['removed'])}): {result['removed']}")
            print(f"modified ({len(result['modified'])}): {result['modified']}")
            if result["diff_summary"]:
                print("\n" + result["diff_summary"])
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
