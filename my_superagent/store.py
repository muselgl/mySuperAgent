from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
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
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
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
                    UNIQUE(task_id, sequence)
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
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(tasks)")}
            for name, default in (("planner", "local"), ("final_summary", "")):
                if name not in columns:
                    connection.execute(f"ALTER TABLE tasks ADD COLUMN {name} TEXT NOT NULL DEFAULT '{default}'")

    def create_task(self, task: Task) -> Task:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO tasks (
                    id, thread_id, instruction, repository, workspace, status,
                    risk_level, plan_json, replan_count, report_sequence,
                    created_at, updated_at, planner, final_summary
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )
        return task

    def save_task(self, task: Task) -> None:
        task.updated_at = now_iso()
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE tasks SET status = ?, risk_level = ?, plan_json = ?,
                    replan_count = ?, report_sequence = ?, updated_at = ?, planner = ?, final_summary = ?
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
        )

    def list_tasks(self) -> list[Task]:
        with self.connection() as connection:
            rows = connection.execute("SELECT id FROM tasks ORDER BY updated_at DESC").fetchall()
        return [self.get_task(row["id"]) for row in rows if self.get_task(row["id"]) is not None]

    def append_event(self, task_id: str, event_type: str, payload: dict) -> int:
        with self.connection() as connection:
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

    def record_report(self, report: WorkerReport) -> None:
        task = self.get_task(report.task_id)
        if task is None:
            raise KeyError(f"Unknown task: {report.task_id}")
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
