"""CLI entry point and daemon loop."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time

from claude_usage.aggregator import (
    aggregate_usage,
    format_report_table,
    get_last_api_snapshot,
    write_status_cache,
)
from claude_usage.calibrator import compute_ratio, ingest_history
from claude_usage.db import DEFAULT_DB_PATH, TrackerDB
from claude_usage.fetcher import FETCH_INTERVAL, fetch_and_store
from claude_usage.scanner import backfill_conversation_boundaries, scan_and_ingest

log = logging.getLogger("claude_usage")


def cmd_run(args: argparse.Namespace) -> None:
    """Polling daemon — scans for new log lines and fetches API usage."""
    db = TrackerDB(args.db)
    interval = args.interval
    fetch_interval = args.fetch_interval

    stop = False

    def handle_signal(signum, frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    log.info(
        "Starting usage tracker daemon (scan=%ds, fetch=%ds, db=%s)",
        interval, fetch_interval, args.db,
    )

    # Initial scan
    count = scan_and_ingest(db)
    if count:
        log.info("Initial scan: ingested %d events", count)
    backfill_conversation_boundaries(db)

    # Initial API fetch
    if not args.no_fetch:
        try:
            data = fetch_and_store(db)
            if data:
                log.info("Initial API fetch successful")
        except Exception:
            log.exception("Error during initial API fetch")

    last_fetch = time.monotonic()

    while not stop:
        time.sleep(interval)

        # Scan cycle
        try:
            count = scan_and_ingest(db)
            if count:
                log.debug("Ingested %d events", count)
            # Update enriched cache with calibrated estimates
            write_status_cache(db)
        except Exception:
            log.exception("Error during scan cycle")

        # Fetch cycle (every fetch_interval seconds)
        if not args.no_fetch and (time.monotonic() - last_fetch) >= fetch_interval:
            last_fetch = time.monotonic()
            try:
                data = fetch_and_store(db)
                if data:
                    log.debug("API fetch successful")
            except Exception:
                log.exception("Error during API fetch")

    log.info("Shutting down")
    db.close()


def cmd_status(args: argparse.Namespace) -> None:
    """Show current aggregated usage."""
    db = TrackerDB(args.db)
    api_snapshot = get_last_api_snapshot()
    report = aggregate_usage(db, api_snapshot)
    db.close()

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(format_report_table(report))


def cmd_session(args: argparse.Namespace) -> None:
    """Show per-session token breakdown."""
    db = TrackerDB(args.db)

    if args.session_id:
        totals = db.get_session_tokens(args.session_id)
        print(json.dumps({"session_id": args.session_id, "tokens": totals}, indent=2))
    else:
        sessions = db.get_active_sessions(since=args.since)
        if args.json:
            print(json.dumps(sessions, indent=2))
        else:
            if not sessions:
                print("No sessions found.")
            else:
                print(f"\n{'Session ID':<40} {'Messages':>8} {'Tokens':>12} {'Last Active'}")
                print("─" * 85)
                for s in sessions:
                    from claude_usage.aggregator import format_tokens_human

                    print(
                        f"{s['session_id']:<40} "
                        f"{s['message_count']:>8} "
                        f"{format_tokens_human(s['total_tokens']):>12} "
                        f"{s['last_seen']}"
                    )
            print()

    db.close()


def cmd_ingest(args: argparse.Namespace) -> None:
    """One-shot: scan all files and ingest."""
    db = TrackerDB(args.db)
    count = scan_and_ingest(db)
    print(f"Ingested {count} events")
    files = db.get_tracked_files()
    print(f"Tracking {len(files)} files")
    db.close()


def cmd_fetch(args: argparse.Namespace) -> None:
    """One-shot: fetch API usage data."""
    db = TrackerDB(args.db)
    data = fetch_and_store(db, force=args.force)
    db.close()

    if data is None:
        print("Fetch skipped or failed (check logs with -v)")
        sys.exit(1)

    five = data.get("five_hour", {})
    seven = data.get("seven_day", {})
    print(f"5h: {five.get('utilization', 0):.1f}%  |  7d: {seven.get('utilization', 0):.1f}%")
    print(f"Fetched at: {data.get('fetched_at')}")

    if args.json:
        print(json.dumps(data, indent=2))


def cmd_serve(args: argparse.Namespace) -> None:
    """Start the web dashboard server."""
    from claude_usage.web import run_server

    db = TrackerDB(args.db, check_same_thread=False)
    run_server(db, host=args.host, port=args.port)
    db.close()


def cmd_config(args: argparse.Namespace) -> None:
    """Get or set config values."""
    db = TrackerDB(args.db)
    if args.value is not None:
        db.set_config(args.key, args.value)
        print(f"{args.key} = {args.value}")
    else:
        val = db.get_config(args.key)
        if val is None:
            print(f"{args.key}: (not set)")
        else:
            print(f"{args.key} = {val}")
    db.close()


def cmd_calibrate(args: argparse.Namespace) -> None:
    """Ingest history.jsonl and show calibration ratios."""
    from claude_usage.calibrator import HISTORY_FILE

    db = TrackerDB(args.db)
    history = Path(args.history) if args.history else HISTORY_FILE

    count = ingest_history(db, history)
    print(f"Ingested {count} new calibration points")

    # Show current ratios
    for window in ("5h", "7d"):
        cal = compute_ratio(db, window)
        if cal:
            print(
                f"  {window} ratio: {cal.ratio:.10f} util%/token "
                f"(confidence: {cal.confidence:.0%}, {cal.data_points} points)"
            )
        else:
            print(f"  {window} ratio: no data")

    db.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="claude-usage-tracker",
        description="Realtime Claude Code usage estimation from session logs",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=str(DEFAULT_DB_PATH),
        help="Path to SQLite database",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging",
    )

    sub = parser.add_subparsers(dest="command")

    # run
    p_run = sub.add_parser("run", help="Start polling daemon")
    p_run.add_argument(
        "--interval",
        type=int,
        default=5,
        help="Scan interval in seconds (default: 5)",
    )
    p_run.add_argument(
        "--fetch-interval",
        type=int,
        default=FETCH_INTERVAL,
        help=f"API fetch interval in seconds (default: {FETCH_INTERVAL})",
    )
    p_run.add_argument(
        "--no-fetch",
        action="store_true",
        help="Disable API fetching (scan-only mode)",
    )

    # status
    p_status = sub.add_parser("status", help="Show current usage")
    p_status.add_argument("--json", action="store_true", help="Output JSON")

    # session
    p_session = sub.add_parser("session", help="Show per-session breakdown")
    p_session.add_argument("session_id", nargs="?", help="Specific session ID")
    p_session.add_argument("--since", type=str, help="Only sessions active since (ISO)")
    p_session.add_argument("--json", action="store_true", help="Output JSON")

    # ingest
    sub.add_parser("ingest", help="One-shot scan and ingest")

    # fetch
    p_fetch = sub.add_parser("fetch", help="One-shot API fetch")
    p_fetch.add_argument("--force", action="store_true", help="Ignore backoff timer")
    p_fetch.add_argument("--json", action="store_true", help="Output full JSON response")

    # serve
    p_serve = sub.add_parser("serve", help="Start web dashboard")
    p_serve.add_argument("--port", type=int, default=2725, help="Listen port (default: 2725)")
    p_serve.add_argument("--host", type=str, default="0.0.0.0", help="Listen host (default: 0.0.0.0)")

    # config
    p_config = sub.add_parser("config", help="Get or set config values (e.g. plan_tier)")
    p_config.add_argument("key", help="Config key (e.g. plan_tier)")
    p_config.add_argument("value", nargs="?", help="Value to set (omit to read)")

    # calibrate
    p_cal = sub.add_parser("calibrate", help="Ingest history and show calibration ratios")
    p_cal.add_argument("--history", type=str, help="Path to history.jsonl")

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.command == "run":
        cmd_run(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "session":
        cmd_session(args)
    elif args.command == "ingest":
        cmd_ingest(args)
    elif args.command == "fetch":
        cmd_fetch(args)
    elif args.command == "serve":
        cmd_serve(args)
    elif args.command == "config":
        cmd_config(args)
    elif args.command == "calibrate":
        cmd_calibrate(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
