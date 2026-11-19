from __future__ import annotations

from contextlib import contextmanager
import json
import hashlib
from pathlib import Path
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterator

from .models import (
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Plan,
    RiskLevel,
    Task,
    TaskStatus,
    WorkerReport,
    EventEnvelope,
    Lease,
    OutboxMessage,
    dumps,
    new_id,
    now_iso,
)


class TaskStore:
    def __init__(self, path: str | Path = ".superagent/state.db", on_event: Callable[[str, dict], None] | None = None) -> None:
        self.path = Path(path)
        self.on_event = on_event
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA foreign_keys=ON")
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    instruction TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    status TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    plan_json TEXT,
                    replan_count INTEGER NOT NULL DEFAULT 0,
                    report_sequence INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    event_id TEXT,
                    idempotency_key TEXT,
                    UNIQUE(task_id, sequence), UNIQUE(event_id), UNIQUE(idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_updated_at ON tasks(updated_at);
                CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, sequence);
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    id TEXT PRIMARY KEY,
                    workspace TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chat_turns (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    instruction TEXT NOT NULL,
                    status TEXT NOT NULL,
                    action_count INTEGER NOT NULL DEFAULT 0,
                    reply TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    retry_of TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chat_messages (
                    session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(session_id, sequence)
                );
                CREATE INDEX IF NOT EXISTS idx_chat_turns_session ON chat_turns(session_id);
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS threads (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, external_key TEXT,
                    repository TEXT, status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(kind, external_key)
                );
                CREATE TABLE IF NOT EXISTS triggers (
                    id TEXT PRIMARY KEY, thread_id TEXT, source TEXT NOT NULL,
                    trigger_type TEXT NOT NULL, payload TEXT NOT NULL,
                    thread_sequence INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                    idempotency_key TEXT UNIQUE
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, worker_id TEXT,
                    fencing_token INTEGER NOT NULL, status TEXT NOT NULL,
                    step_id TEXT, started_at TEXT NOT NULL, finished_at TEXT, error TEXT
                );
                CREATE TABLE IF NOT EXISTS leases (
                    task_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, owner TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL, expires_at TEXT NOT NULL,
                    acquired_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    message_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, target TEXT NOT NULL,
                    payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    delivery_attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
                    next_attempt_at TEXT, acknowledged_at TEXT, created_at TEXT NOT NULL,
                    UNIQUE(event_id, target)
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, run_id TEXT,
                    artifact_type TEXT NOT NULL, path_or_key TEXT NOT NULL,
                    sha256 TEXT NOT NULL, size INTEGER NOT NULL, metadata TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, run_id TEXT, step_id TEXT,
                    summary TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    fencing_token INTEGER NOT NULL, decided_by TEXT, decided_at TEXT,
                    expires_at TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS subscriptions (
                    thread_id TEXT NOT NULL, worker_id TEXT NOT NULL,
                    consumer_sequence INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                    PRIMARY KEY(thread_id, worker_id)
                );
                CREATE TABLE IF NOT EXISTS dependencies (
                    task_id TEXT NOT NULL, depends_on_task_id TEXT NOT NULL,
                    relation TEXT NOT NULL DEFAULT 'depends_on', allow_failure INTEGER NOT NULL DEFAULT 0,
                    reason TEXT, PRIMARY KEY(task_id, depends_on_task_id)
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, run_id TEXT,
                    step_id TEXT, fencing_token INTEGER NOT NULL, state TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS environments (
                    id TEXT PRIMARY KEY, task_id TEXT, path TEXT NOT NULL,
                    repository TEXT, metadata TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS locks (
                    id TEXT PRIMARY KEY, environment_id TEXT NOT NULL, owner TEXT NOT NULL,
                    run_id TEXT, mode TEXT NOT NULL, fencing_token INTEGER NOT NULL,
                    acquired_at TEXT NOT NULL, expires_at TEXT, released_at TEXT, release_reason TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox(status, next_attempt_at);
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(tasks)")}
            for name, definition in (("planner", "TEXT NOT NULL DEFAULT 'local'"), ("final_summary", "TEXT NOT NULL DEFAULT ''"),
                                     ("environment_id", "TEXT"), ("active_run_id", "TEXT"),
                                     ("lease_owner", "TEXT"), ("lease_expires_at", "TEXT"),
                                     ("fencing_token", "INTEGER NOT NULL DEFAULT 0"),
                                     ("retry_count", "INTEGER NOT NULL DEFAULT 0"),
                                     ("recovery_count", "INTEGER NOT NULL DEFAULT 0")):
                if name not in columns:
                    connection.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
            connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_active_run ON tasks(id) WHERE active_run_id IS NOT NULL")
            connection.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version', '2')")
            event_columns = {row["name"] for row in connection.execute("PRAGMA table_info(events)")}
            if "event_id" not in event_columns:
                connection.execute("ALTER TABLE events ADD COLUMN event_id TEXT")
            if "idempotency_key" not in event_columns:
                connection.execute("ALTER TABLE events ADD COLUMN idempotency_key TEXT")

    @property
    def schema_version(self) -> int:
        with self.connection() as connection:
            row = connection.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
        return int(row["value"]) if row else 1

    transaction = connection

    def save_task_event(self, task: Task, event_type: str, payload: dict) -> int:
        """Update the task snapshot and append its event in one SQLite transaction."""
        task.updated_at = now_iso()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE tasks SET status=?, risk_level=?, plan_json=?, replan_count=?, report_sequence=?, updated_at=?, planner=?, final_summary=?, environment_id=?, active_run_id=?, lease_owner=?, lease_expires_at=?, fencing_token=?, retry_count=?, recovery_count=? WHERE id=?",
                (task.status.value, task.risk_level.value, dumps(task.plan.to_dict()) if task.plan else None, task.replan_count, task.report_sequence, task.updated_at, task.planner, task.final_summary, task.environment_id, task.active_run_id, task.lease_owner, task.lease_expires_at, task.fencing_token, task.retry_count, task.recovery_count, task.id),
            )
            row = connection.execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM events WHERE task_id=?", (task.id,)).fetchone()
            sequence = int(row[0])
            connection.execute("INSERT INTO events(task_id, sequence, event_type, payload, created_at) VALUES (?, ?, ?, ?, ?)", (task.id, sequence, event_type, dumps(payload), now_iso()))
        if self.on_event:
            self.on_event(event_type, payload)
        return sequence

    def create_task(self, task: Task) -> Task:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO tasks (
                    id, thread_id, instruction, repository, workspace, status,
                    risk_level, plan_json, replan_count, report_sequence,
                    created_at, updated_at, planner, final_summary, environment_id,
                    active_run_id, lease_owner, lease_expires_at, fencing_token, retry_count, recovery_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.id,
                    task.thread_id,
                    task.instruction,
                    task.repository,
                    task.workspace,
                    task.status.value,
                    task.risk_level.value,
                    dumps(task.plan.to_dict()) if task.plan else None,
                    task.replan_count,
                    task.report_sequence,
                    task.created_at,
                    task.updated_at,
                    task.planner,
                    task.final_summary,
                    task.environment_id, task.active_run_id, task.lease_owner, task.lease_expires_at,
                    task.fencing_token, task.retry_count, task.recovery_count,
                ),
            )
        return task

    def save_task(self, task: Task) -> None:
        task.updated_at = now_iso()
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE tasks SET status = ?, risk_level = ?, plan_json = ?,
                    replan_count = ?, report_sequence = ?, updated_at = ?, planner = ?, final_summary = ?,
                    environment_id = ?, active_run_id = ?, lease_owner = ?, lease_expires_at = ?,
                    fencing_token = ?, retry_count = ?, recovery_count = ?
                WHERE id = ?
                """,
                (
                    task.status.value,
                    task.risk_level.value,
                    dumps(task.plan.to_dict()) if task.plan else None,
                    task.replan_count,
                    task.report_sequence,
                    task.updated_at,
                    task.planner,
                    task.final_summary,
                    task.environment_id, task.active_run_id, task.lease_owner, task.lease_expires_at,
                    task.fencing_token, task.retry_count, task.recovery_count,
                    task.id,
                ),
            )

    def get_task(self, task_id: str) -> Task | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return None
        plan = Plan.from_dict(json.loads(row["plan_json"])) if row["plan_json"] else None
        return Task(
            id=row["id"],
            thread_id=row["thread_id"],
            instruction=row["instruction"],
            repository=row["repository"],
            workspace=row["workspace"],
            status=TaskStatus(row["status"]),
            risk_level=RiskLevel(row["risk_level"]),
            plan=plan,
            replan_count=row["replan_count"],
            report_sequence=row["report_sequence"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            planner=row["planner"],
            final_summary=row["final_summary"],
            environment_id=row["environment_id"], active_run_id=row["active_run_id"],
            lease_owner=row["lease_owner"], lease_expires_at=row["lease_expires_at"],
            fencing_token=row["fencing_token"], retry_count=row["retry_count"], recovery_count=row["recovery_count"],
        )

    def list_tasks(self) -> list[Task]:
        with self.connection() as connection:
            rows = connection.execute("SELECT id FROM tasks ORDER BY updated_at DESC").fetchall()
        return [self.get_task(row["id"]) for row in rows if self.get_task(row["id"]) is not None]

    def append_event(self, task_id: str, event_type: str, payload: dict) -> int:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM events WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            sequence = int(row["next_sequence"])
            connection.execute(
                "INSERT INTO events(task_id, sequence, event_type, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (task_id, sequence, event_type, dumps(payload), now_iso()),
            )
        if self.on_event:
            self.on_event(event_type, payload)
        return sequence

    def append_envelope(self, event: EventEnvelope) -> EventEnvelope:
        """Persist an event and its outbox delivery atomically."""
        if event.task_id is None:
            raise ValueError("Event envelope requires task_id for task event storage")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if event.idempotency_key:
                existing = connection.execute(
                    "SELECT sequence, event_id FROM events WHERE idempotency_key = ?",
                    (event.idempotency_key,),
                ).fetchone()
                if existing:
                    event.sequence = int(existing["sequence"])
                    event.event_id = existing["event_id"] or event.event_id
                    return event
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM events WHERE task_id = ?", (event.task_id,)
            ).fetchone()
            event.sequence = int(row[0])
            connection.execute(
                "INSERT INTO events(task_id, sequence, event_type, payload, created_at, event_id, idempotency_key) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (event.task_id, event.sequence, event.event_type, dumps(event.to_dict()), event.created_at,
                 event.event_id, event.idempotency_key),
            )
            connection.execute(
                "INSERT OR IGNORE INTO outbox(message_id, event_id, target, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (new_id("msg"), event.event_id, event.source, dumps(event.to_dict()), event.created_at),
            )
        if self.on_event:
            self.on_event(event.event_type, event.to_dict())
        return event

    def enqueue_outbox(self, event_id: str, target: str, payload: dict) -> str:
        message_id = new_id("msg")
        with self.connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO outbox(message_id, event_id, target, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (message_id, event_id, target, dumps(payload), now_iso()),
            )
            row = connection.execute("SELECT message_id FROM outbox WHERE event_id = ? AND target = ?", (event_id, target)).fetchone()
        return row["message_id"]

    def claim_outbox(self, target: str | None = None, limit: int = 100) -> list[OutboxMessage]:
        now = now_iso()
        with self.connection() as connection:
            query = "SELECT * FROM outbox WHERE status IN ('pending', 'published') AND (next_attempt_at IS NULL OR next_attempt_at <= ?)"
            params: list[object] = [now]
            if target:
                query += " AND target = ?"
                params.append(target)
            query += " ORDER BY created_at LIMIT ?"
            params.append(limit)
            rows = connection.execute(query, params).fetchall()
            messages: list[OutboxMessage] = []
            for row in rows:
                connection.execute("UPDATE outbox SET status = 'published', delivery_attempts = delivery_attempts + 1 WHERE message_id = ?", (row["message_id"],))
                messages.append(OutboxMessage(row["message_id"], row["event_id"], row["target"], json.loads(row["payload"]), "published", row["delivery_attempts"] + 1, row["last_error"], row["next_attempt_at"], row["acknowledged_at"], row["created_at"]))
        return messages

    def ack_outbox(self, message_id: str) -> bool:
        with self.connection() as connection:
            return connection.execute("UPDATE outbox SET status='acked', acknowledged_at=? WHERE message_id=? AND status != 'acked'", (now_iso(), message_id)).rowcount > 0

    def fail_outbox(self, message_id: str, error: str, max_attempts: int = 5) -> str:
        with self.connection() as connection:
            row = connection.execute("SELECT delivery_attempts FROM outbox WHERE message_id = ?", (message_id,)).fetchone()
            if row is None:
                raise KeyError(message_id)
            attempts = int(row["delivery_attempts"])
            status = "dead_letter" if attempts >= max_attempts else "pending"
            delay = min(60, 2 ** max(0, attempts - 1))
            next_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
            connection.execute("UPDATE outbox SET status=?, last_error=?, next_attempt_at=? WHERE message_id=?", (status, error[:2000], next_at, message_id))
        return status

    def replay_events(self, *, task_id: str | None = None, thread_id: str | None = None, run_id: str | None = None, since: str | None = None) -> list[dict]:
        events = self.events(task_id) if task_id else []
        if not task_id:
            with self.connection() as connection:
                rows = connection.execute("SELECT sequence, event_type, payload, created_at FROM events ORDER BY id").fetchall()
            events = [{"sequence": row["sequence"], "event_type": row["event_type"], "payload": json.loads(row["payload"]), "created_at": row["created_at"]} for row in rows]
        def matches(item: dict) -> bool:
            payload = item["payload"]
            if thread_id and payload.get("thread_id") != thread_id:
                return False
            if run_id and payload.get("run_id") != run_id:
                return False
            return not since or item["created_at"] >= since
        return [item for item in events if matches(item)]

    def claim_task(self, owner: str, *, lease_seconds: int = 60, run_id: str | None = None) -> Lease | None:
        run_id = run_id or new_id("run")
        now = datetime.now(timezone.utc)
        expires = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM tasks WHERE status IN ('queued','running') AND (active_run_id IS NULL OR lease_expires_at IS NULL OR lease_expires_at <= ?) ORDER BY updated_at LIMIT 1",
                (now.isoformat(),),
            ).fetchone()
            if row is None:
                return None
            token = int(row["fencing_token"]) + 1
            updated = connection.execute(
                "UPDATE tasks SET status='running', active_run_id=?, lease_owner=?, lease_expires_at=?, fencing_token=?, updated_at=? WHERE id=? AND (active_run_id IS NULL OR lease_expires_at IS NULL OR lease_expires_at <= ?)",
                (run_id, owner, expires, token, now.isoformat(), row["id"], now.isoformat()),
            )
            if updated.rowcount != 1:
                return None
            connection.execute("INSERT OR REPLACE INTO leases(task_id, run_id, owner, fencing_token, expires_at, acquired_at) VALUES (?, ?, ?, ?, ?, ?)", (row["id"], run_id, owner, token, expires, now.isoformat()))
            connection.execute("INSERT INTO runs(id, task_id, worker_id, fencing_token, status, started_at) VALUES (?, ?, ?, ?, 'running', ?)", (run_id, row["id"], owner, token, now.isoformat()))
        return Lease(row["id"], run_id, owner, token, expires)

    acquire_lease = claim_task

    def heartbeat(self, lease: Lease, *, lease_seconds: int = 60) -> bool:
        now = datetime.now(timezone.utc)
        expires = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self.connection() as connection:
            updated = connection.execute("UPDATE tasks SET lease_expires_at=? WHERE id=? AND active_run_id=? AND lease_owner=? AND fencing_token=? AND status='running' AND (lease_expires_at IS NULL OR lease_expires_at > ?)", (expires, lease.task_id, lease.run_id, lease.owner, lease.fencing_token, now.isoformat())).rowcount
            if updated:
                connection.execute("UPDATE leases SET expires_at=? WHERE task_id=? AND run_id=? AND fencing_token=?", (expires, lease.task_id, lease.run_id, lease.fencing_token))
        return bool(updated)

    renew_lease = heartbeat

    def release_lease(self, lease: Lease, status: TaskStatus | None = None) -> bool:
        with self.connection() as connection:
            if status is None:
                query = "UPDATE tasks SET active_run_id=NULL, lease_owner=NULL, lease_expires_at=NULL, updated_at=? WHERE id=? AND active_run_id=? AND lease_owner=? AND fencing_token=?"
                values = (now_iso(), lease.task_id, lease.run_id, lease.owner, lease.fencing_token)
            else:
                query = "UPDATE tasks SET active_run_id=NULL, lease_owner=NULL, lease_expires_at=NULL, status=?, updated_at=? WHERE id=? AND active_run_id=? AND lease_owner=? AND fencing_token=?"
                values = (status.value, now_iso(), lease.task_id, lease.run_id, lease.owner, lease.fencing_token)
            updated = connection.execute(query, values).rowcount
            if updated:
                connection.execute("UPDATE leases SET expires_at=? WHERE task_id=? AND run_id=? AND fencing_token=?", (now_iso(), lease.task_id, lease.run_id, lease.fencing_token))
                connection.execute("UPDATE runs SET status=?, finished_at=? WHERE id=? AND fencing_token=?", ((status.value if status else 'finished'), now_iso(), lease.run_id, lease.fencing_token))
        return bool(updated)

    def record_checkpoint(self, task_id: str, state: dict, **kwargs: object) -> str:
        return self.save_checkpoint(task_id, state, **kwargs)

    def fenced_update(self, lease: Lease, sql: str, params: tuple = ()) -> bool:
        with self.connection() as connection:
            row = connection.execute("SELECT 1 FROM tasks WHERE id=? AND active_run_id=? AND lease_owner=? AND fencing_token=? AND status='running'", (lease.task_id, lease.run_id, lease.owner, lease.fencing_token)).fetchone()
            if row is None:
                return False
            connection.execute(sql, params)
        return True

    def create_thread(self, kind: str = "task", external_key: str | None = None, repository: str | None = None) -> str:
        thread_id = new_id("thr")
        with self.connection() as connection:
            connection.execute("INSERT INTO threads(id, kind, external_key, repository, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)", (thread_id, kind, external_key, repository, now_iso(), now_iso()))
        return thread_id

    def append_trigger(self, thread_id: str, source: str, trigger_type: str, payload: dict, idempotency_key: str | None = None) -> str:
        trigger_id = new_id("trg")
        with self.connection() as connection:
            if idempotency_key:
                existing = connection.execute("SELECT id FROM triggers WHERE idempotency_key=?", (idempotency_key,)).fetchone()
                if existing:
                    return existing["id"]
            row = connection.execute("SELECT COALESCE(MAX(thread_sequence), 0) + 1 FROM triggers WHERE thread_id=?", (thread_id,)).fetchone()
            connection.execute("INSERT INTO triggers(id, thread_id, source, trigger_type, payload, thread_sequence, created_at, idempotency_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (trigger_id, thread_id, source, trigger_type, dumps(payload), int(row[0]), now_iso(), idempotency_key))
        return trigger_id

    def save_checkpoint(self, task_id: str, state: dict, *, run_id: str | None = None, step_id: str | None = None, fencing_token: int = 0) -> str:
        checkpoint_id = new_id("chk")
        with self.connection() as connection:
            connection.execute("INSERT INTO checkpoints(id, task_id, run_id, step_id, fencing_token, state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (checkpoint_id, task_id, run_id, step_id, fencing_token, dumps(state), now_iso()))
        return checkpoint_id

    def latest_checkpoint(self, task_id: str, run_id: str | None = None) -> dict | None:
        with self.connection() as connection:
            query = "SELECT * FROM checkpoints WHERE task_id=?"
            params: list[object] = [task_id]
            if run_id:
                query += " AND run_id=?"
                params.append(run_id)
            row = connection.execute(query + " ORDER BY created_at DESC LIMIT 1", params).fetchone()
        return {**dict(row), "state": json.loads(row["state"])} if row else None

    def create_artifact(self, task_id: str, artifact_type: str, path_or_key: str, content: bytes | None = None, *, run_id: str | None = None, metadata: dict | None = None) -> str:
        artifact_id = new_id("artifact")
        if content is None:
            content = Path(path_or_key).read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        with self.connection() as connection:
            connection.execute("INSERT INTO artifacts(id, task_id, run_id, artifact_type, path_or_key, sha256, size, metadata, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (artifact_id, task_id, run_id, artifact_type, path_or_key, digest, len(content), dumps(metadata or {}), now_iso()))
        return artifact_id

    def create_approval(self, task_id: str, summary: str, *, run_id: str | None = None, step_id: str | None = None, fencing_token: int = 0, expires_at: str | None = None) -> str:
        approval_id = new_id("approval")
        with self.connection() as connection:
            connection.execute("INSERT INTO approvals(id, task_id, run_id, step_id, summary, fencing_token, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (approval_id, task_id, run_id, step_id, summary, fencing_token, expires_at, now_iso()))
        return approval_id

    def decide_approval(self, approval_id: str, decision: str, decided_by: str = "user") -> bool:
        if decision not in {"approved", "rejected", "request_changes"}:
            raise ValueError("invalid approval decision")
        with self.connection() as connection:
            return connection.execute("UPDATE approvals SET status=?, decided_by=?, decided_at=? WHERE id=? AND status='pending'", (decision, decided_by, now_iso(), approval_id)).rowcount > 0

    def acquire_lock(self, environment_id: str, owner: str, *, mode: str = "write", run_id: str | None = None, fencing_token: int = 0, ttl_seconds: int = 300) -> str | None:
        if mode not in {"read", "write"}:
            raise ValueError("lock mode must be read or write")
        lock_id = new_id("lock")
        expires = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute("SELECT mode FROM locks WHERE environment_id=? AND released_at IS NULL AND (expires_at IS NULL OR expires_at > ?)", (environment_id, now_iso())).fetchall()
            if any(mode == "write" or row["mode"] == "write" for row in active):
                return None
            connection.execute("INSERT INTO locks(id, environment_id, owner, run_id, mode, fencing_token, acquired_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (lock_id, environment_id, owner, run_id, mode, fencing_token, now_iso(), expires))
        return lock_id

    def release_lock(self, lock_id: str, reason: str = "released") -> bool:
        with self.connection() as connection:
            return connection.execute("UPDATE locks SET released_at=?, release_reason=? WHERE id=? AND released_at IS NULL", (now_iso(), reason, lock_id)).rowcount > 0

    def add_dependency(self, task_id: str, depends_on_task_id: str, relation: str = "depends_on", allow_failure: bool = False) -> None:
        with self.connection() as connection:
            connection.execute("INSERT OR REPLACE INTO dependencies(task_id, depends_on_task_id, relation, allow_failure) VALUES (?, ?, ?, ?)", (task_id, depends_on_task_id, relation, int(allow_failure)))

    def subscribe(self, thread_id: str, worker_id: str) -> None:
        with self.connection() as connection:
            connection.execute("INSERT OR IGNORE INTO subscriptions(thread_id, worker_id, created_at) VALUES (?, ?, ?)", (thread_id, worker_id, now_iso()))

    def unsubscribe(self, thread_id: str, worker_id: str) -> bool:
        with self.connection() as connection:
            return connection.execute("DELETE FROM subscriptions WHERE thread_id=? AND worker_id=?", (thread_id, worker_id)).rowcount > 0

    def subscriptions(self, thread_id: str) -> list[dict]:
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM subscriptions WHERE thread_id=? ORDER BY worker_id", (thread_id,)).fetchall()
        return [dict(row) for row in rows]

    def dependencies_ready(self, task_id: str) -> tuple[bool, str | None]:
        with self.connection() as connection:
            rows = connection.execute("SELECT d.*, t.status FROM dependencies d JOIN tasks t ON t.id=d.depends_on_task_id WHERE d.task_id=?", (task_id,)).fetchall()
        for row in rows:
            if row["status"] in {"failed", "cancelled", "blocked"} and not row["allow_failure"]:
                return False, f"dependency {row['depends_on_task_id']} is {row['status']}"
            if row["status"] != "completed" and not row["allow_failure"]:
                return False, f"dependency {row['depends_on_task_id']} is {row['status']}"
        return True, None

    def record_report(self, report: WorkerReport) -> None:
        task = self.get_task(report.task_id)
        if task is None:
            raise KeyError(f"Unknown task: {report.task_id}")
        if report.run_id and (task.active_run_id != report.run_id or task.fencing_token != report.fencing_token):
            raise RuntimeError("stale fencing token")
        if report.sequence < task.report_sequence:
            return
        if report.sequence == task.report_sequence and any(item["payload"].get("sequence") == report.sequence and item["event_type"] == report.event_type for item in self.events(report.task_id)):
            return
        task.report_sequence = report.sequence
        self.save_task(task)
        self.append_event(report.task_id, report.event_type, report.to_dict())

    def events(self, task_id: str) -> list[dict]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT sequence, event_type, payload, created_at FROM events WHERE task_id = ? ORDER BY sequence",
                (task_id,),
            ).fetchall()
        return [
            {
                "sequence": row["sequence"],
                "event_type": row["event_type"],
                "payload": json.loads(row["payload"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def create_chat_session(self, workspace: str) -> ChatSession:
        session = ChatSession(new_id("chat"), workspace)
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO chat_sessions VALUES (?, ?, ?, ?)",
                (session.id, session.workspace, session.created_at, session.updated_at),
            )
        return session

    def get_chat_session(self, session_id: str) -> ChatSession | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM chat_sessions WHERE id = ?", (session_id,)).fetchone()
        return ChatSession(**dict(row)) if row else None

    @staticmethod
    def _chat_turn(row: sqlite3.Row) -> ChatTurn:
        value = dict(row)
        value["status"] = ChatTurnStatus(value["status"])
        return ChatTurn(**value)

    def chat_turns(self, session_id: str) -> list[ChatTurn]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM chat_turns WHERE session_id = ? ORDER BY rowid", (session_id,),
            ).fetchall()
        return [self._chat_turn(row) for row in rows]

    def get_chat_turn(self, turn_id: str) -> ChatTurn:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM chat_turns WHERE id = ?", (turn_id,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown chat turn: {turn_id}")
        return self._chat_turn(row)

    def start_chat_turn(self, session_id: str, text: str, retry_of: str | None = None) -> ChatTurn:
        turn = ChatTurn(new_id("turn"), session_id, text, retry_of=retry_of)
        with self.connection() as connection:
            if connection.execute("SELECT id FROM chat_sessions WHERE id = ?", (session_id,)).fetchone() is None:
                raise ValueError(f"Unknown chat session: {session_id}")
            connection.execute(
                """INSERT INTO chat_turns
                   (id, session_id, instruction, status, retry_of, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (turn.id, session_id, text, turn.status.value, retry_of, turn.created_at, turn.updated_at),
            )
            self._append_chat_message(connection, turn, "user", {"type": "message", "content": text})
        return turn

    def _append_chat_message(self, connection: sqlite3.Connection, turn: ChatTurn, role: str, payload: dict) -> None:
        row = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 AS seq FROM chat_messages WHERE session_id = ?",
            (turn.session_id,),
        ).fetchone()
        timestamp = now_iso()
        connection.execute(
            "INSERT INTO chat_messages VALUES (?, ?, ?, ?, ?, ?)",
            (turn.session_id, turn.id, row["seq"], role, dumps(payload), timestamp),
        )
        connection.execute("UPDATE chat_sessions SET updated_at = ? WHERE id = ?", (timestamp, turn.session_id))

    def append_chat_message(self, turn_id: str, role: str, payload: dict) -> None:
        turn = self.get_chat_turn(turn_id)
        with self.connection() as connection:
            self._append_chat_message(connection, turn, role, payload)

    def begin_chat_action(self, turn_id: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE chat_turns SET action_count = action_count + 1, updated_at = ? WHERE id = ?",
                (now_iso(), turn_id),
            )

    def finish_chat_turn(self, turn_id: str, status: ChatTurnStatus, reply: str = "", error: str = "") -> ChatTurn:
        turn = self.get_chat_turn(turn_id)
        if turn.status != ChatTurnStatus.RUNNING:
            return turn
        with self.connection() as connection:
            if status == ChatTurnStatus.COMPLETED:
                self._append_chat_message(connection, turn, "assistant", {"type": "reply", "content": reply})
            else:
                self._append_chat_message(connection, turn, "event", {"type": "turn_status", "status": status.value, "error": error})
            connection.execute(
                "UPDATE chat_turns SET status = ?, reply = ?, error = ?, updated_at = ? WHERE id = ?",
                (status.value, reply, error, now_iso(), turn_id),
            )
        return self.get_chat_turn(turn_id)

    def interrupt_chat_turns(self, session_id: str) -> list[ChatTurn]:
        return [
            self.finish_chat_turn(turn.id, ChatTurnStatus.INTERRUPTED, error="上次执行中断，可使用 /retry 重试")
            for turn in self.chat_turns(session_id) if turn.status == ChatTurnStatus.RUNNING
        ]

    def chat_messages(self, session_id: str) -> list[dict]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM chat_messages WHERE session_id = ? ORDER BY sequence", (session_id,),
            ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]
