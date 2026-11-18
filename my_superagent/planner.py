from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .models import Decision, Plan, PlanBudget, RiskLevel, Step, StepStatus, Task, ToolCall, new_id


class Planner(Protocol):
    def create_plan(self, task: Task) -> Plan: ...

    def select_action(self, task: Task, step: Step) -> Decision: ...


class LocalPlanner:
    """Deterministic planner used until a model-backed planner is connected."""

    def create_plan(self, task: Task) -> Plan:
        instruction = task.instruction.strip()
        steps = [
            Step(
                id=new_id("step"),
                objective="检查工作区并收集任务上下文",
                actions=["workspace.inspect"],
                expected_outputs=["工作区文件列表", "Git 状态"],
                verifier={"type": "result_ok"},
                status=StepStatus.READY,
            )
        ]
        previous_step_id = steps[0].id
        if (Path(task.workspace) / ".git").exists():
            steps.append(
                Step(
                    id=new_id("step"),
                    objective="读取 Git 工作区状态",
                    dependencies=[previous_step_id],
                    actions=["git.status"],
                    expected_outputs=["Git 状态"],
                    verifier={"type": "result_ok"},
                )
            )
            previous_step_id = steps[-1].id
        lowered = instruction.lower()
        if "test" in lowered or "测试" in instruction or "pytest" in lowered:
            steps.append(
                Step(
                    id=new_id("step"),
                    objective="运行项目测试",
                    dependencies=[previous_step_id],
                    actions=["shell.run"],
                    expected_outputs=["测试退出码和输出"],
                    verifier={"type": "result_ok"},
                )
            )
        return Plan(
            id=new_id("plan"),
            task_id=task.id,
            version=1,
            goal=instruction,
            acceptance_criteria=["所有计划步骤完成", "每个步骤都有验证结果"],
            risk_level=task.risk_level,
            budget=PlanBudget(),
            steps=steps,
        )

    def select_action(self, task: Task, step: Step) -> Decision:
        del task
        if "shell.run" in step.actions:
            command = "python -m pytest -q" if Path("pytest.ini").exists() or Path("pyproject.toml").exists() else "python -m unittest discover -s tests"
            return Decision(type="call_tool", tool_call=ToolCall("shell.run", {"command": command}))
        if "workspace.inspect" in step.actions:
            return Decision(type="call_tool", tool_call=ToolCall("workspace.inspect"))
        if "git.status" in step.actions:
            return Decision(type="call_tool", tool_call=ToolCall("git.status"))
        return Decision(type="report_progress", summary=f"步骤待处理：{step.objective}")
