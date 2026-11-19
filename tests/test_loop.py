from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from my_superagent.loop import LoopController
from my_superagent.models import RiskLevel, Task, TaskStatus, new_id
from my_superagent.planner import LocalPlanner
from my_superagent.store import TaskStore
from my_superagent.tools import default_registry


class LoopControllerTests(unittest.TestCase):
    def test_task_runs_workspace_inspection_to_completion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sample.txt").write_text("hello", encoding="utf-8")
            store = TaskStore(root / "state.db")
            task = Task(
                id=new_id("task"),
                thread_id=new_id("thread"),
                instruction="检查这个工作区",
                repository=str(root),
                workspace=str(root),
                risk_level=RiskLevel.READ_ONLY,
            )
            store.create_task(task)

            result = LoopController(store, default_registry(), LocalPlanner()).run_task(task.id)

            self.assertEqual(result.status, TaskStatus.COMPLETED)
            persisted = store.get_task(task.id)
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertEqual(persisted.status, TaskStatus.COMPLETED)
            self.assertEqual(persisted.plan.steps[0].status.value, "succeeded")
            self.assertTrue(any(event["event_type"] == "task_completed" for event in store.events(task.id)))

    def test_unknown_tool_fails_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = TaskStore(root / "state.db")
            task = Task(
                id=new_id("task"),
                thread_id=new_id("thread"),
                instruction="检查这个工作区",
                repository=str(root),
                workspace=str(root),
            )
            store.create_task(task)

            class BrokenPlanner(LocalPlanner):
                def select_action(self, task, step):
                    from my_superagent.models import Decision, ToolCall

                    return Decision(type="call_tool", tool_call=ToolCall("missing.tool"))

            result = LoopController(store, default_registry(), BrokenPlanner()).run_task(task.id)
            self.assertEqual(result.status, TaskStatus.FAILED)


if __name__ == "__main__":
    unittest.main()
