from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
import json
from typing import Any
from uuid import uuid4


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    BLOCKED = "blocked"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ChatTurnStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    INTERRUPTED = "interrupted"


@dataclass
class ChatSession:
    id: str
    workspace: str
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)


@dataclass
class ChatTurn:
    id: str
    session_id: str
    instruction: str
    status: ChatTurnStatus = ChatTurnStatus.RUNNING
    action_count: int = 0
    reply: str = ""
    error: str = ""
    retry_of: str | None = None
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)


class StepStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class RiskLevel(StrEnum):
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    REMOTE_WRITE = "remote_write"


@dataclass
class PlanBudget:
    max_steps: int = 20
    max_replans: int = 3
    max_wall_time_minutes: int = 30


@dataclass
class Step:
    id: str
    objective: str
    dependencies: list[str] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    expected_outputs: list[str] = field(default_factory=list)
    verifier: dict[str, Any] = field(default_factory=lambda: {"type": "result_ok"})
    retry_policy: dict[str, Any] = field(default_factory=lambda: {"max_attempts": 2})
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    result: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Step":
        value = dict(value)
        value["status"] = StepStatus(value.get("status", StepStatus.PENDING))
        return cls(**value)


@dataclass
class Plan:
    id: str
    task_id: str
    version: int
    goal: str
    acceptance_criteria: list[str]
    risk_level: RiskLevel
    budget: PlanBudget = field(default_factory=PlanBudget)
    steps: list[Step] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.id,
            "task_id": self.task_id,
            "version": self.version,
            "goal": self.goal,
            "acceptance_criteria": self.acceptance_criteria,
            "risk_level": self.risk_level.value,
            "budget": asdict(self.budget),
            "steps": [step.to_dict() for step in self.steps],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Plan":
        return cls(
            id=value["plan_id"],
            task_id=value["task_id"],
            version=value["version"],
            goal=value["goal"],
            acceptance_criteria=list(value.get("acceptance_criteria", [])),
            risk_level=RiskLevel(value.get("risk_level", RiskLevel.READ_ONLY)),
            budget=PlanBudget(**value.get("budget", {})),
            steps=[Step.from_dict(item) for item in value.get("steps", [])],
        )


@dataclass
class Task:
    id: str
    thread_id: str
    instruction: str
    repository: str
    workspace: str
    status: TaskStatus = TaskStatus.QUEUED
    risk_level: RiskLevel = RiskLevel.READ_ONLY
    plan: Plan | None = None
    replan_count: int = 0
    report_sequence: int = 0
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    planner: str = "local"
    final_summary: str = ""


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str = field(default_factory=lambda: new_id("idem"))


@dataclass
class ToolResult:
    ok: bool
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    exit_code: int | None = None
    duration_ms: int = 0


@dataclass
class Decision:
    type: str
    tool_call: ToolCall | None = None
    summary: str = ""
    request: dict[str, Any] | None = None


@dataclass
class WorkerReport:
    task_id: str
    sequence: int
    event_type: str
    status: str
    plan_version: int | None
    step_id: str | None
    summary: str
    artifacts: list[str] = field(default_factory=list)
    blocking_reason: str | None = None
    next_action: str | None = None
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
