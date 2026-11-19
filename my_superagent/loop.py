from __future__ import annotations

from dataclasses import dataclass
import time

from .models import (
    Decision,
    Plan,
    Step,
    StepStatus,
    Task,
    TaskStatus,
    ToolResult,
    WorkerReport,
    new_id,
)
from .planner import Planner
from .store import TaskStore
from .tools import ToolRegistry


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class VerificationResult:
    ok: bool
    summary: str
    evidence: dict


class LoopController:
    def __init__(self, store: TaskStore, tools: ToolRegistry, planner: Planner) -> None:
        self.store = store
        self.tools = tools
        self.planner = planner

    def run_task(self, task_id: str) -> Task:
        task = self._require_task(task_id)
        if task.status in {TaskStatus.COMPLETED, TaskStatus.CANCELLED}:
            return task
        started = time.monotonic()
        task.status = TaskStatus.RUNNING
        self.store.save_task(task)
        self.store.append_event(task.id, "task_started", {"status": task.status.value})

        try:
            if task.plan is None:
                task.plan = self.planner.create_plan(task)
                self.store.save_task(task)
                self.store.append_event(task.id, "plan_created", task.plan.to_dict())

            for step in task.plan.steps:
                if step.status == StepStatus.RUNNING and "model.loop" in step.actions:
                    step.status = StepStatus.READY

            while True:
                step = self._select_ready_step(task.plan)
                if step is None:
                    task.status = TaskStatus.COMPLETED
                    self.store.save_task(task)
                    self._report(task, "task_completed", task.final_summary or "任务已完成，所有计划步骤均已验证")
                    return task

                self._check_budget(task, started)
                step.status = StepStatus.RUNNING
                step.attempts += 1
                self.store.save_task(task)
                self.store.append_event(task.id, "step_started", {"step_id": step.id, "objective": step.objective})
                decision = self.planner.select_action(task, step)
                self._apply_decision(task, step, decision)
                if step.status == StepStatus.SUCCEEDED:
                    self._report(task, "step_reported", self._step_summary(step))

        except BudgetExceeded as error:
            task.status = TaskStatus.BLOCKED
            self.store.save_task(task)
            self._report(task, "task_blocked", str(error), blocking_reason=str(error))
            return task
        except Exception as error:
            task.status = TaskStatus.FAILED
            self.store.save_task(task)
            self._report(task, "task_failed", str(error), blocking_reason=str(error))
            return task

    def _apply_decision(self, task: Task, step: Step, decision: Decision) -> None:
        if decision.type == "call_tool":
            if decision.tool_call is None:
                raise ValueError("call_tool decision requires tool_call")
            run_id = new_id("run")
            self.store.append_event(
                task.id,
                "tool_started",
                {"run_id": run_id, "step_id": step.id, "tool": decision.tool_call.name, "arguments": decision.tool_call.arguments},
            )
            result = self.tools.execute(decision.tool_call, task.workspace)
            self.store.append_event(
                task.id,
                "tool_finished",
                {"run_id": run_id, "step_id": step.id, "tool": decision.tool_call.name, "result": result.output, "ok": result.ok, "error": result.error},
            )
            verification = self._verify(step, result)
            if "model.loop" in step.actions:
                if step.result is None:
                    step.result = {"history": []}
                step.result["history"].append({
                    "tool": decision.tool_call.name, "arguments": decision.tool_call.arguments,
                    "ok": result.ok, "output": result.output, "error": result.error,
                    "verification": verification.evidence,
                })
                step.status = StepStatus.READY
                self.store.save_task(task)
                return
            step.result = {"tool": decision.tool_call.name, "result": result.output, "verification": verification.evidence}
            step.status = StepStatus.SUCCEEDED if verification.ok else StepStatus.FAILED
            if not verification.ok:
                raise RuntimeError(f"步骤验证失败：{verification.summary}")
            self.store.save_task(task)
            return

        if decision.type == "report_progress":
            step.status = StepStatus.SUCCEEDED
            step.result = {"summary": decision.summary}
            self.store.save_task(task)
            return

        if decision.type == "finish":
            step.status = StepStatus.SUCCEEDED
            if step.result is None:
                step.result = {}
            step.result["summary"] = decision.summary
            if "model.summary" in step.actions:
                task.final_summary = decision.summary
            self.store.save_task(task)
            return

        raise ValueError(f"Unsupported decision: {decision.type}")

    def _verify(self, step: Step, result: ToolResult) -> VerificationResult:
        if step.verifier.get("type") == "result_ok":
            return VerificationResult(
                ok=result.ok,
                summary="工具执行成功" if result.ok else (result.error or "工具执行失败"),
                evidence={"ok": result.ok, "exit_code": result.exit_code, "error": result.error},
            )
        return VerificationResult(ok=result.ok, summary="默认结果验证", evidence={"ok": result.ok})

    def _select_ready_step(self, plan: Plan) -> Step | None:
        succeeded = {step.id for step in plan.steps if step.status == StepStatus.SUCCEEDED}
        for step in plan.steps:
            if step.status == StepStatus.FAILED:
                max_attempts = int(step.retry_policy.get("max_attempts", 1))
                if step.attempts < max_attempts:
                    step.status = StepStatus.READY
                else:
                    raise RuntimeError(f"步骤超过最大重试次数：{step.objective}")
            if step.status == StepStatus.PENDING and all(dep in succeeded for dep in step.dependencies):
                step.status = StepStatus.READY
            if step.status == StepStatus.READY:
                return step
        return None

    def _check_budget(self, task: Task, started: float) -> None:
        plan = task.plan
        if plan is None:
            return
        executed = sum(step.attempts for step in plan.steps)
        if executed >= plan.budget.max_steps:
            raise BudgetExceeded("已达到任务最大步骤数")
        if time.monotonic() - started > plan.budget.max_wall_time_minutes * 60:
            raise BudgetExceeded("已达到任务最大执行时间")

    def _report(self, task: Task, event_type: str, summary: str, blocking_reason: str | None = None) -> None:
        task.report_sequence += 1
        self.store.save_task(task)
        report = WorkerReport(
            task_id=task.id,
            sequence=task.report_sequence,
            event_type=event_type,
            status=task.status.value,
            plan_version=task.plan.version if task.plan else None,
            step_id=self._current_step_id(task),
            summary=summary,
            blocking_reason=blocking_reason,
        )
        self.store.record_report(report)

    @staticmethod
    def _current_step_id(task: Task) -> str | None:
        if task.plan is None:
            return None
        for step in task.plan.steps:
            if step.status in {StepStatus.RUNNING, StepStatus.READY, StepStatus.FAILED, StepStatus.BLOCKED}:
                return step.id
        return task.plan.steps[-1].id if task.plan.steps else None

    @staticmethod
    def _step_summary(step: Step) -> str:
        return f"步骤 {step.objective} 已完成" if step.status == StepStatus.SUCCEEDED else f"步骤 {step.objective} 状态为 {step.status.value}"

    def _require_task(self, task_id: str) -> Task:
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError(f"Unknown task: {task_id}")
        return task
