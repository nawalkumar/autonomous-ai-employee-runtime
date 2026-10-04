"""SQLite persistence for ExecutionState snapshots and append-only audit events."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from app.models.events import AuditEvent
from app.models.state import ExecutionState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    state_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    action TEXT,
    tool TEXT,
    actor TEXT NOT NULL,
    arguments_json TEXT,
    result_json TEXT,
    observation_json TEXT,
    failure_json TEXT,
    verification_result_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_task_id_id ON events (task_id, id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks (status);
"""


def _json_dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, default=str)


def _json_loads(value: str | None) -> Any:
    if value is None:
        return None
    return json.loads(value)


class SQLiteStore:
    """Small repository over SQLite for task state and audit events."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        if self.db_path.parent and str(self.db_path.parent) not in ("", "."):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def save_state(self, state: ExecutionState) -> None:
        """Upsert a full ExecutionState snapshot for a task."""
        payload = state.model_dump_json()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tasks (task_id, status, created_at, updated_at, state_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    status = excluded.status,
                    updated_at = excluded.updated_at,
                    state_json = excluded.state_json
                """,
                (
                    state.task_id,
                    state.final_status.value,
                    state.created_at.isoformat(),
                    state.updated_at.isoformat(),
                    payload,
                ),
            )

    def get_state(self, task_id: str) -> ExecutionState | None:
        """Load the latest ExecutionState snapshot, or None if missing."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state_json FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        return ExecutionState.model_validate_json(row["state_json"])

    def append_event(self, event: AuditEvent) -> None:
        """Insert an immutable audit event. Duplicate event_id raises."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO events (
                    event_id, task_id, timestamp, event_type, action, tool, actor,
                    arguments_json, result_json, observation_json,
                    failure_json, verification_result_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.task_id,
                    event.timestamp.isoformat(),
                    event.event_type,
                    event.action,
                    event.tool,
                    event.actor.value,
                    _json_dumps(event.arguments),
                    _json_dumps(event.result),
                    _json_dumps(event.observation),
                    _json_dumps(event.failure),
                    _json_dumps(event.verification_result),
                ),
            )

    def get_events(self, task_id: str) -> list[AuditEvent]:
        """Return audit events for a task in insertion order."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    event_id, task_id, timestamp, event_type, action, tool, actor,
                    arguments_json, result_json, observation_json,
                    failure_json, verification_result_json
                FROM events
                WHERE task_id = ?
                ORDER BY id ASC
                """,
                (task_id,),
            ).fetchall()

        events: list[AuditEvent] = []
        for row in rows:
            events.append(
                AuditEvent(
                    event_id=row["event_id"],
                    task_id=row["task_id"],
                    timestamp=row["timestamp"],
                    event_type=row["event_type"],
                    action=row["action"],
                    tool=row["tool"],
                    actor=row["actor"],
                    arguments=_json_loads(row["arguments_json"]),
                    result=_json_loads(row["result_json"]),
                    observation=_json_loads(row["observation_json"]),
                    failure=_json_loads(row["failure_json"]),
                    verification_result=_json_loads(row["verification_result_json"]),
                )
            )
        return events
