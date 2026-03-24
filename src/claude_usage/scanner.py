"""File discovery and incremental log reader."""

from __future__ import annotations

import logging
from pathlib import Path

from claude_usage.db import TrackerDB
from claude_usage.models import AgentCompletion, UsageSnapshot
from claude_usage.parser import parse_line

log = logging.getLogger(__name__)

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Files to skip
SKIP_FILENAMES = {"history.jsonl"}


def discover_jsonl_files(
    projects_dir: Path = CLAUDE_PROJECTS_DIR,
) -> list[Path]:
    """Find all session .jsonl files across all project directories.

    Returns main session files and subagent files, excluding history.jsonl.
    """
    files = []
    if not projects_dir.is_dir():
        return files

    for jsonl in projects_dir.rglob("*.jsonl"):
        if jsonl.name in SKIP_FILENAMES:
            continue
        # Skip meta files
        if jsonl.suffix != ".jsonl":
            continue
        files.append(jsonl)

    return sorted(files)


def read_new_lines(file_path: Path, offset: int) -> tuple[list[str], int]:
    """Read new complete lines from a file starting at byte offset.

    Returns (lines, new_offset). Only returns complete lines (ending with newline)
    to avoid reading partial writes.
    """
    try:
        file_size = file_path.stat().st_size
    except OSError:
        return [], offset

    if file_size < offset:
        # File was truncated — reset
        log.warning("File truncated, resetting offset: %s", file_path)
        offset = 0

    if file_size == offset:
        return [], offset

    lines = []
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            data = f.read()

            # Only process complete lines
            if not data.endswith("\n"):
                last_nl = data.rfind("\n")
                if last_nl == -1:
                    # No complete lines yet
                    return [], offset
                data = data[: last_nl + 1]

            new_offset = offset + len(data.encode("utf-8"))
            lines = [line for line in data.splitlines() if line.strip()]
    except OSError as e:
        log.warning("Error reading %s: %s", file_path, e)
        return [], offset

    return lines, new_offset


# Strings that must appear in a line for it to be worth JSON-parsing.
# Lines without these substrings can't produce UsageSnapshot or AgentCompletion.
_INTERESTING_MARKERS = ('"type":"assistant"', '"type": "assistant"',
                        '"agent_progress"',
                        '"totalTokens"')


def _line_might_be_interesting(line: str) -> bool:
    """Fast pre-filter: skip lines that can't contain usage data."""
    return any(m in line for m in _INTERESTING_MARKERS)


def scan_and_ingest(db: TrackerDB, projects_dir: Path = CLAUDE_PROJECTS_DIR) -> int:
    """One scan cycle: discover files, read new lines, parse and store.

    Returns the number of new events ingested.
    """
    files = discover_jsonl_files(projects_dir)
    total_events = 0

    for file_path in files:
        fp_str = str(file_path)
        offset = db.get_file_offset(fp_str)
        lines, new_offset = read_new_lines(file_path, offset)

        if not lines:
            continue

        events = 0
        db.begin()
        try:
            for line in lines:
                if not _line_might_be_interesting(line):
                    continue

                parsed = parse_line(line)
                if parsed is None:
                    continue

                if isinstance(parsed, UsageSnapshot):
                    db.upsert_snapshot(parsed, auto_commit=False)
                    events += 1
                elif isinstance(parsed, AgentCompletion):
                    db.record_agent_completion(parsed, auto_commit=False)
                    events += 1

            db.set_file_offset(fp_str, new_offset, file_path.stat().st_mtime)
            db.commit()
        except Exception:
            db.conn.rollback()
            log.exception("Error ingesting %s", file_path)
            continue

        total_events += events

        if events:
            log.debug("Ingested %d events from %s", events, file_path.name)

    return total_events
