"""SQLite schema, migrations, and data access layer."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Sequence

from claude_usage.models import AgentCompletion, ConversationBoundary, UsageDelta, UsageSnapshot

DEFAULT_DB_PATH = Path.home() / ".local" / "state" / "claude-usage" / "tracker.db"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ingest_state (
    file_path   TEXT PRIMARY KEY,
    byte_offset INTEGER NOT NULL DEFAULT 0,
    last_modified REAL,
    last_scanned_at TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    logical_key TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    actor_id    TEXT NOT NULL,
    actor_type  TEXT NOT NULL,
    source_type TEXT NOT NULL,
    message_id  TEXT NOT NULL,
    model       TEXT,
    stop_reason TEXT,
    timestamp   TEXT NOT NULL,
    request_id  TEXT,
    input_tokens              INTEGER NOT NULL DEFAULT 0,
    output_tokens             INTEGER NOT NULL DEFAULT 0,
    cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_input_tokens   INTEGER NOT NULL DEFAULT 0,
    total_tokens              INTEGER NOT NULL DEFAULT 0,
    is_provisional INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS deltas (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    logical_key TEXT NOT NULL,
    timestamp   TEXT NOT NULL,
    delta_input              INTEGER NOT NULL DEFAULT 0,
    delta_output             INTEGER NOT NULL DEFAULT 0,
    delta_cache_creation     INTEGER NOT NULL DEFAULT 0,
    delta_cache_read         INTEGER NOT NULL DEFAULT 0,
    delta_total              INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_deltas_timestamp ON deltas(timestamp);
CREATE INDEX IF NOT EXISTS idx_deltas_logical_key ON deltas(logical_key);
CREATE INDEX IF NOT EXISTS idx_snapshots_session ON snapshots(session_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_timestamp ON snapshots(timestamp);

CREATE TABLE IF NOT EXISTS agent_completions (
    agent_id    TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    total_tokens         INTEGER NOT NULL DEFAULT 0,
    total_duration_ms    INTEGER NOT NULL DEFAULT 0,
    total_tool_use_count INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT NOT NULL,
    input_tokens              INTEGER NOT NULL DEFAULT 0,
    output_tokens             INTEGER NOT NULL DEFAULT 0,
    cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_input_tokens   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS calibration (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,
    estimated_tokens_5h  INTEGER,
    estimated_tokens_7d  INTEGER,
    official_util_5h     REAL,
    official_util_7d     REAL,
    plan_tier   TEXT
);

CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS promotions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    start_at    TEXT NOT NULL,
    end_at      TEXT NOT NULL,
    multiplier  REAL NOT NULL DEFAULT 1.0,
    description TEXT
);

CREATE TABLE IF NOT EXISTS session_projects (
    session_id   TEXT PRIMARY KEY,
    project_hash TEXT NOT NULL,
    project_name TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_projects_name ON session_projects(project_name);

CREATE TABLE IF NOT EXISTS conversation_boundaries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    timestamp   TEXT NOT NULL,
    trigger     TEXT NOT NULL DEFAULT 'unknown',
    UNIQUE(session_id, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_conv_boundaries_session ON conversation_boundaries(session_id);
"""


class TrackerDB:
    """SQLite data access for the usage tracker."""

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH, *, check_same_thread: bool = True):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=check_same_thread)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(SCHEMA_SQL)
        self._migrate()

    def _migrate(self) -> None:
        """Run lightweight migrations for schema additions."""
        # Add plan_tier column to calibration if missing
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(calibration)")}
        if "plan_tier" not in cols:
            self.conn.execute("ALTER TABLE calibration ADD COLUMN plan_tier TEXT")
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # --- Config ---

    def get_config(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM config WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else default

    def set_config(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    # --- File offset tracking ---

    def get_file_offset(self, file_path: str) -> int:
        row = self.conn.execute(
            "SELECT byte_offset FROM ingest_state WHERE file_path = ?",
            (file_path,),
        ).fetchone()
        return row[0] if row else 0

    def set_file_offset(
        self, file_path: str, offset: int, last_modified: float | None = None
    ) -> None:
        self.conn.execute(
            """INSERT INTO ingest_state (file_path, byte_offset, last_modified, last_scanned_at)
               VALUES (?, ?, ?, datetime('now'))
               ON CONFLICT(file_path) DO UPDATE SET
                 byte_offset = excluded.byte_offset,
                 last_modified = excluded.last_modified,
                 last_scanned_at = excluded.last_scanned_at""",
            (file_path, offset, last_modified),
        )
        self.conn.commit()

    # --- Snapshot upsert with delta ---

    def begin(self) -> None:
        """Begin an explicit transaction for batched writes."""
        self.conn.execute("BEGIN")

    def commit(self) -> None:
        """Commit the current transaction."""
        self.conn.commit()

    def upsert_snapshot(self, snap: UsageSnapshot, *, auto_commit: bool = True) -> UsageDelta | None:
        """Insert or update a snapshot. Returns the delta if tokens changed."""
        existing = self.conn.execute(
            "SELECT input_tokens, output_tokens, cache_creation_input_tokens, "
            "cache_read_input_tokens, total_tokens, is_provisional "
            "FROM snapshots WHERE logical_key = ?",
            (snap.logical_key,),
        ).fetchone()

        if existing:
            old_input, old_output, old_cache_create, old_cache_read, old_total, old_prov = existing

            # Authoritative source supersedes provisional
            if old_prov == 0 and snap.is_provisional:
                return None  # don't overwrite authoritative with provisional

            d_input = max(0, snap.input_tokens - old_input)
            d_output = max(0, snap.output_tokens - old_output)
            d_cache_create = max(0, snap.cache_creation_input_tokens - old_cache_create)
            d_cache_read = max(0, snap.cache_read_input_tokens - old_cache_read)
            d_total = max(0, snap.total_tokens - old_total)

            # No change
            if d_total == 0 and d_input == 0 and d_output == 0:
                return None

            delta = UsageDelta(
                logical_key=snap.logical_key,
                timestamp=snap.timestamp,
                delta_input=d_input,
                delta_output=d_output,
                delta_cache_creation=d_cache_create,
                delta_cache_read=d_cache_read,
                delta_total=d_total,
            )
        else:
            # First snapshot — entire value is the delta
            delta = UsageDelta(
                logical_key=snap.logical_key,
                timestamp=snap.timestamp,
                delta_input=snap.input_tokens,
                delta_output=snap.output_tokens,
                delta_cache_creation=snap.cache_creation_input_tokens,
                delta_cache_read=snap.cache_read_input_tokens,
                delta_total=snap.total_tokens,
            )

        # Upsert snapshot
        self.conn.execute(
            """INSERT INTO snapshots
               (logical_key, session_id, actor_id, actor_type, source_type,
                message_id, model, stop_reason, timestamp, request_id,
                input_tokens, output_tokens, cache_creation_input_tokens,
                cache_read_input_tokens, total_tokens, is_provisional, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(logical_key) DO UPDATE SET
                 source_type = excluded.source_type,
                 model = excluded.model,
                 stop_reason = excluded.stop_reason,
                 timestamp = excluded.timestamp,
                 request_id = excluded.request_id,
                 input_tokens = excluded.input_tokens,
                 output_tokens = excluded.output_tokens,
                 cache_creation_input_tokens = excluded.cache_creation_input_tokens,
                 cache_read_input_tokens = excluded.cache_read_input_tokens,
                 total_tokens = excluded.total_tokens,
                 is_provisional = excluded.is_provisional,
                 updated_at = excluded.updated_at""",
            (
                snap.logical_key, snap.session_id, snap.actor_id,
                snap.actor_type.value, snap.source_type.value,
                snap.message_id, snap.model, snap.stop_reason,
                snap.timestamp, snap.request_id,
                snap.input_tokens, snap.output_tokens,
                snap.cache_creation_input_tokens, snap.cache_read_input_tokens,
                snap.total_tokens, int(snap.is_provisional),
            ),
        )

        # Insert delta
        self.conn.execute(
            """INSERT INTO deltas
               (logical_key, timestamp, delta_input, delta_output,
                delta_cache_creation, delta_cache_read, delta_total)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                delta.logical_key, delta.timestamp,
                delta.delta_input, delta.delta_output,
                delta.delta_cache_creation, delta.delta_cache_read,
                delta.delta_total,
            ),
        )

        if auto_commit:
            self.conn.commit()
        return delta

    # --- Agent completions ---

    def record_agent_completion(self, comp: AgentCompletion, *, auto_commit: bool = True) -> None:
        self.conn.execute(
            """INSERT INTO agent_completions
               (agent_id, session_id, total_tokens, total_duration_ms,
                total_tool_use_count, completed_at,
                input_tokens, output_tokens,
                cache_creation_input_tokens, cache_read_input_tokens)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(agent_id) DO UPDATE SET
                 total_tokens = excluded.total_tokens,
                 total_duration_ms = excluded.total_duration_ms,
                 total_tool_use_count = excluded.total_tool_use_count,
                 completed_at = excluded.completed_at,
                 input_tokens = excluded.input_tokens,
                 output_tokens = excluded.output_tokens,
                 cache_creation_input_tokens = excluded.cache_creation_input_tokens,
                 cache_read_input_tokens = excluded.cache_read_input_tokens""",
            (
                comp.agent_id, comp.session_id, comp.total_tokens,
                comp.total_duration_ms, comp.total_tool_use_count,
                comp.completed_at,
                comp.input_tokens, comp.output_tokens,
                comp.cache_creation_input_tokens, comp.cache_read_input_tokens,
            ),
        )
        if auto_commit:
            self.conn.commit()

    # --- Conversation boundaries ---

    def record_conversation_boundary(self, boundary: ConversationBoundary, *, auto_commit: bool = True) -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO conversation_boundaries
               (session_id, timestamp, trigger)
               VALUES (?, ?, ?)""",
            (boundary.session_id, boundary.timestamp, boundary.trigger),
        )
        if auto_commit:
            self.conn.commit()

    def get_conversation_counts(self) -> dict[str, int]:
        """Return {session_id: conversation_count} for all sessions.

        Conversations = 1 (initial) + number of compact boundaries.
        """
        rows = self.conn.execute(
            """SELECT session_id, COUNT(*) as boundaries
            FROM conversation_boundaries
            GROUP BY session_id"""
        ).fetchall()
        return {r[0]: r[1] + 1 for r in rows}

    def get_total_conversation_count(self, since: str | None = None) -> int:
        """Total conversations across all sessions, optionally filtered by time.

        For sessions with no boundaries in the table, each session = 1 conversation.
        """
        conditions = []
        params: list[str] = []
        if since:
            conditions.append("s.timestamp >= ?")
            params.append(since)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        # Get distinct session count and boundary count in one query
        row = self.conn.execute(
            f"""SELECT
                COUNT(DISTINCT s.session_id) as session_count,
                COALESCE((
                    SELECT COUNT(*)
                    FROM conversation_boundaries cb
                    WHERE cb.session_id IN (
                        SELECT DISTINCT s2.session_id FROM snapshots s2 {where}
                    )
                ), 0) as boundary_count
            FROM snapshots s
            {where}""",
            params + params,
        ).fetchone()

        return (row[0] or 0) + (row[1] or 0)

    # --- Aggregation queries ---

    def get_cumulative_tokens(
        self, since: str | None = None, until: str | None = None
    ) -> dict[str, int]:
        """Sum all deltas in a time window. Returns dict of token type → total."""
        conditions = []
        params: list[str] = []
        if since:
            conditions.append("timestamp >= ?")
            params.append(since)
        if until:
            conditions.append("timestamp <= ?")
            params.append(until)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        row = self.conn.execute(
            f"""SELECT
                COALESCE(SUM(delta_input), 0),
                COALESCE(SUM(delta_output), 0),
                COALESCE(SUM(delta_cache_creation), 0),
                COALESCE(SUM(delta_cache_read), 0),
                COALESCE(SUM(delta_total), 0)
            FROM deltas {where}""",
            params,
        ).fetchone()

        return {
            "input_tokens": row[0],
            "output_tokens": row[1],
            "cache_creation_input_tokens": row[2],
            "cache_read_input_tokens": row[3],
            "total_tokens": row[4],
        }

    def get_session_tokens(self, session_id: str) -> dict[str, int]:
        """Sum all deltas for a specific session."""
        row = self.conn.execute(
            """SELECT
                COALESCE(SUM(d.delta_input), 0),
                COALESCE(SUM(d.delta_output), 0),
                COALESCE(SUM(d.delta_cache_creation), 0),
                COALESCE(SUM(d.delta_cache_read), 0),
                COALESCE(SUM(d.delta_total), 0)
            FROM deltas d
            JOIN snapshots s ON d.logical_key = s.logical_key
            WHERE s.session_id = ?""",
            (session_id,),
        ).fetchone()

        return {
            "input_tokens": row[0],
            "output_tokens": row[1],
            "cache_creation_input_tokens": row[2],
            "cache_read_input_tokens": row[3],
            "total_tokens": row[4],
        }

    def get_model_breakdown(
        self, since: str | None = None, until: str | None = None
    ) -> dict[str, dict[str, int]]:
        """Token totals grouped by model within a time window."""
        conditions = []
        params: list[str] = []
        if since:
            conditions.append("d.timestamp >= ?")
            params.append(since)
        if until:
            conditions.append("d.timestamp <= ?")
            params.append(until)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        rows = self.conn.execute(
            f"""SELECT s.model,
                COALESCE(SUM(d.delta_input), 0),
                COALESCE(SUM(d.delta_output), 0),
                COALESCE(SUM(d.delta_cache_creation), 0),
                COALESCE(SUM(d.delta_cache_read), 0),
                COALESCE(SUM(d.delta_total), 0)
            FROM deltas d
            JOIN snapshots s ON d.logical_key = s.logical_key
            {where}
            GROUP BY s.model""",
            params,
        ).fetchall()

        result = {}
        for row in rows:
            result[row[0] or "unknown"] = {
                "input_tokens": row[1],
                "output_tokens": row[2],
                "cache_creation_input_tokens": row[3],
                "cache_read_input_tokens": row[4],
                "total_tokens": row[5],
            }
        return result

    def get_active_sessions(self, since: str | None = None) -> Sequence[dict]:
        """List sessions with activity in the given window."""
        conditions = []
        params: list[str] = []
        if since:
            conditions.append("timestamp >= ?")
            params.append(since)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        rows = self.conn.execute(
            f"""SELECT session_id,
                MIN(timestamp) as first_seen,
                MAX(timestamp) as last_seen,
                COUNT(DISTINCT logical_key) as message_count,
                COALESCE(SUM(total_tokens), 0) as total_tokens
            FROM snapshots
            {where}
            GROUP BY session_id
            ORDER BY last_seen DESC""",
            params,
        ).fetchall()

        return [
            {
                "session_id": r[0],
                "first_seen": r[1],
                "last_seen": r[2],
                "message_count": r[3],
                "total_tokens": r[4],
            }
            for r in rows
        ]

    def get_tracked_files(self) -> Sequence[dict]:
        """List all files being tracked with their offsets."""
        rows = self.conn.execute(
            "SELECT file_path, byte_offset, last_scanned_at FROM ingest_state ORDER BY last_scanned_at DESC"
        ).fetchall()
        return [
            {"file_path": r[0], "byte_offset": r[1], "last_scanned_at": r[2]}
            for r in rows
        ]

    # --- Session-to-project mapping ---

    def refresh_session_projects(self) -> int:
        """Rebuild session_projects from ingest_state file paths.

        Path pattern: ~/.claude/projects/<hash>/<session>.jsonl
        Also handles subagents/: ~/.claude/projects/<hash>/subagents/<session>.jsonl
        """
        import re

        rows = self.conn.execute("SELECT file_path FROM ingest_state").fetchall()
        count = 0
        for (fp,) in rows:
            # Extract project hash and session ID from path
            m = re.search(r"/\.claude/projects/([^/]+)/(?:subagents/)?([^/]+)\.jsonl$", fp)
            if not m:
                continue
            project_hash, session_id = m.group(1), m.group(2)
            # Convert hash to human name: strip home dir prefixes
            # Project hashes look like "-home-user-develop-project-name"
            name = project_hash
            home = Path.home().as_posix().replace("/", "-")  # e.g. "-home-user"
            for suffix in ("-develop-", "-"):
                prefix = home + suffix
                if name.startswith(prefix):
                    name = name[len(prefix):]
                    break
            self.conn.execute(
                "INSERT OR IGNORE INTO session_projects (session_id, project_hash, project_name) VALUES (?, ?, ?)",
                (session_id, project_hash, name),
            )
            count += 1
        self.conn.commit()
        return count

    def get_project_breakdown(
        self, since: str | None = None, until: str | None = None
    ) -> list[dict]:
        """Token totals per project within a time window."""
        conditions = []
        params: list[str] = []
        if since:
            conditions.append("d.timestamp >= ?")
            params.append(since)
        if until:
            conditions.append("d.timestamp <= ?")
            params.append(until)

        where = f"AND {' AND '.join(conditions)}" if conditions else ""

        rows = self.conn.execute(
            f"""SELECT COALESCE(sp.project_name, 'unknown') as project,
                COALESCE(SUM(d.delta_total), 0) as total,
                COALESCE(SUM(d.delta_output), 0) as output,
                COUNT(DISTINCT s.session_id) as sessions
            FROM deltas d
            JOIN snapshots s ON d.logical_key = s.logical_key
            LEFT JOIN session_projects sp ON s.session_id = sp.session_id
            WHERE 1=1 {where}
            GROUP BY project
            ORDER BY total DESC""",
            params,
        ).fetchall()

        # Get conversation counts per session for enrichment
        convo_counts = self.get_conversation_counts()

        grand_total = sum(r[1] for r in rows) or 1

        # For each project, sum conversations across its sessions
        # We need session IDs per project for this, so do a secondary lookup
        project_sessions: dict[str, set[str]] = {}
        for r in self.conn.execute(
            f"""SELECT COALESCE(sp.project_name, 'unknown') as project,
                s.session_id
            FROM snapshots s
            LEFT JOIN session_projects sp ON s.session_id = sp.session_id
            JOIN deltas d ON d.logical_key = s.logical_key
            WHERE 1=1 {where}
            GROUP BY project, s.session_id""",
            params,
        ).fetchall():
            project_sessions.setdefault(r[0], set()).add(r[1])

        result = []
        for r in rows:
            project_name = r[0]
            sess_ids = project_sessions.get(project_name, set())
            convo_count = sum(convo_counts.get(sid, 1) for sid in sess_ids)
            result.append({
                "project_name": project_name,
                "total_tokens": r[1],
                "output_tokens": r[2],
                "session_count": r[3],
                "conversation_count": convo_count,
                "pct": round(r[1] / grand_total * 100, 1),
            })
        return result

    def get_daily_totals(self, days: int = 30) -> list[dict]:
        """Daily token aggregation for the last N days."""
        rows = self.conn.execute(
            """SELECT date(timestamp) as day,
                COALESCE(SUM(delta_total), 0),
                COALESCE(SUM(delta_input), 0),
                COALESCE(SUM(delta_output), 0),
                COALESCE(SUM(delta_cache_creation), 0),
                COALESCE(SUM(delta_cache_read), 0)
            FROM deltas
            WHERE timestamp >= date('now', ?)
            GROUP BY day
            ORDER BY day""",
            (f"-{days} days",),
        ).fetchall()

        return [
            {
                "date": r[0],
                "total_tokens": r[1],
                "input_tokens": r[2],
                "output_tokens": r[3],
                "cache_creation_tokens": r[4],
                "cache_read_tokens": r[5],
            }
            for r in rows
        ]

    def get_hourly_totals(self, since: str) -> list[dict]:
        """Hourly token aggregation within a window (for pacing)."""
        rows = self.conn.execute(
            """SELECT strftime('%Y-%m-%dT%H:00:00Z', timestamp) as hour,
                COALESCE(SUM(delta_total), 0),
                COALESCE(SUM(delta_output), 0)
            FROM deltas
            WHERE timestamp >= ?
            GROUP BY hour
            ORDER BY hour""",
            (since,),
        ).fetchall()

        return [
            {"hour": r[0], "total_tokens": r[1], "output_tokens": r[2]}
            for r in rows
        ]

    def get_sessions_with_project(self, limit: int = 50) -> list[dict]:
        """Recent sessions enriched with project name, model, and conversation count."""
        rows = self.conn.execute(
            """SELECT s.session_id,
                COALESCE(sp.project_name, 'unknown') as project,
                MIN(s.timestamp) as first_seen,
                MAX(s.timestamp) as last_seen,
                COUNT(DISTINCT s.logical_key) as message_count,
                COALESCE(SUM(s.total_tokens), 0) as total_tokens,
                s.model,
                1 + COALESCE(cb.boundary_count, 0) as conversation_count
            FROM snapshots s
            LEFT JOIN session_projects sp ON s.session_id = sp.session_id
            LEFT JOIN (
                SELECT session_id, COUNT(*) as boundary_count
                FROM conversation_boundaries
                GROUP BY session_id
            ) cb ON s.session_id = cb.session_id
            GROUP BY s.session_id
            ORDER BY last_seen DESC
            LIMIT ?""",
            (limit,),
        ).fetchall()

        return [
            {
                "session_id": r[0],
                "project_name": r[1],
                "first_seen": r[2],
                "last_seen": r[3],
                "message_count": r[4],
                "total_tokens": r[5],
                "model": r[6],
                "conversation_count": r[7],
            }
            for r in rows
        ]
