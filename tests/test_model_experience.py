from __future__ import annotations

from contextlib import closing, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from my_superagent.cli import main
from my_superagent.config import ModelConfig
from my_superagent.llm import ModelReply
from my_superagent.loop import LoopController
from my_superagent.model_planner import ModelPlanner, PlannerOutputError
from my_superagent.models import StepStatus, Task, TaskStatus, ToolCall
from my_superagent.store import TaskStore
from my_superagent.tools import model_registry


PLAN = {"steps": [{"objective": "阅读项目入口"}], "acceptance_criteria": ["给出代码引用"]}


def tool(name: str, **arguments) -> dict:
    return {"type": "call_tool", "tool_call": {"name": name, "arguments": arguments}}


def finish(summary: str) -> dict:
    return {"type": "finish", "summary": summary}


class ScriptedClient:
    def __init__(self, replies: list) -> None:
        self.replies = iter(replies)
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> ModelReply:
        self.prompts.append(prompt)
        value = next(self.replies)
        return ModelReply(json.dumps(value, ensure_ascii=False), "test", {"total_tokens": 10})


class ModelExperienceTests(unittest.TestCase):
    def make_task(self, root: Path, store: TaskStore) -> Task:
        task = Task("task-test", "thread-test", "分析项目入口", str(root), str(root), planner="model")
        store.create_task(task)
        return task

    def test_cli_watch_runs_tools_receives_failures_and_persists_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.py").write_text('print("hello evidence")\n', encoding="utf-8")
            (root / ".env").write_text("SECRET=hidden", encoding="utf-8")
            db = str(root / ".superagent" / "state.db")
            summary = "项目入口 main.py:1 输出 hello evidence。"
            client = ScriptedClient([
                PLAN, tool("workspace.inspect"), tool("file.read", path="missing.py"),
                tool("file.read", path="main.py"), finish("入口证据 main.py:1"), finish(summary),
            ])
            output = io.StringIO()
            with patch("my_superagent.cli.ModelConfig.load", return_value=ModelConfig("test")), \
                    patch("my_superagent.cli.ModelClient", return_value=client), redirect_stdout(output):
                code = main(["task", "分析项目入口", "--repo", str(root), "--db", db, "--planner", "model", "--watch"])
            self.assertEqual(code, 0)
            self.assertIn("[计划]", output.getvalue())
            self.assertIn("[工具] file.read", output.getvalue())
            self.assertIn("[结果] 失败", output.getvalue())
            self.assertIn(summary, output.getvalue())
            store = TaskStore(db)
            saved = store.list_tasks()[0]
            self.assertEqual(saved.planner, "model")
            self.assertEqual(saved.final_summary, summary)
            self.assertEqual(saved.status, TaskStatus.COMPLETED)
            self.assertTrue(all(step.status == StepStatus.SUCCEEDED for step in saved.plan.steps))
            context = json.loads(client.prompts[4].split("Task context and observations:\n")[1])
            observations = context["observations"]
            self.assertFalse(observations[1]["ok"])
            self.assertIn("1: print", observations[2]["output"]["content"])
            self.assertEqual(observations[2]["arguments"], {"path": "main.py"})
            self.assertNotIn(".env", observations[0]["output"]["files"])
            events = store.events(saved.id)
            self.assertEqual(len([event for event in events if event["event_type"] == "model_finished"]), 6)
            shown = io.StringIO()
            with redirect_stdout(shown):
                self.assertEqual(main(["task", "show", saved.id, "--db", db]), 0)
            self.assertEqual(json.loads(shown.getvalue())["final_summary"], summary)

    def test_saved_planner_used_on_later_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text("evidence", encoding="utf-8")
            db = str(root / "state.db")
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["task", "分析", "--repo", str(root), "--db", db, "--planner", "model"]), 0)
            task_id = output.getvalue().strip()
            client = ScriptedClient([PLAN, tool("file.read", path="README.md"), finish("README.md:1 evidence"), finish("已验证 README.md:1")])
            with patch("my_superagent.cli.ModelConfig.load", return_value=ModelConfig("test")), \
                    patch("my_superagent.cli.ModelClient", return_value=client), redirect_stdout(io.StringIO()):
                self.assertEqual(main(["run", task_id, "--db", db]), 0)
            self.assertEqual(TaskStore(db).get_task(task_id).final_summary, "已验证 README.md:1")

    def test_resume_uses_persisted_observations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = TaskStore(root / "state.db")
            task = self.make_task(root, store)
            planner = ModelPlanner(ScriptedClient([PLAN]))
            task.plan = planner.create_plan(task)
            task.plan.steps[0].status = StepStatus.RUNNING
            task.plan.steps[0].attempts = 1
            task.plan.steps[0].result = {"history": [{"tool": "file.read", "ok": True, "output": {"content": "saved evidence"}}]}
            task.status = TaskStatus.RUNNING
            store.save_task(task)
            client = ScriptedClient([finish("saved evidence"), finish("最终结果")])
            result = LoopController(TaskStore(root / "state.db"), model_registry(), ModelPlanner(client)).run_task(task.id)
            self.assertEqual(result.status, TaskStatus.COMPLETED)
            self.assertIn("saved evidence", client.prompts[0])

    def test_budget_blocks_tool_loop_and_allows_exact_limit_completion(self):
        for complete in (False, True):
            with self.subTest(complete=complete), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                store = TaskStore(root / "state.db")
                task = self.make_task(root, store)
                task.plan = ModelPlanner(ScriptedClient([PLAN])).create_plan(task)
                task.plan.budget.max_steps = 3
                store.save_task(task)
                actions = [tool("workspace.inspect"), finish("已检查"), finish("完成")] if complete else [tool("workspace.inspect")] * 3
                result = LoopController(store, model_registry(), ModelPlanner(ScriptedClient(actions))).run_task(task.id)
                self.assertEqual(result.status, TaskStatus.COMPLETED if complete else TaskStatus.BLOCKED)
                self.assertEqual(sum(step.attempts for step in result.plan.steps), 3)

    def test_invalid_model_plan_and_action_fail_clearly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for value in ({"steps": []}, {"steps": [{"objective": ""}]}, {"steps": ["invalid"]}):
                with self.subTest(value=value), self.assertRaises(PlannerOutputError):
                    ModelPlanner(ScriptedClient([value])).create_plan(Task("t", "h", "分析", str(root), str(root)))
            store = TaskStore(root / "state.db")
            task = self.make_task(root, store)
            client = ScriptedClient([PLAN, tool("shell.run", command="python malicious.py")])
            result = LoopController(store, model_registry(), ModelPlanner(client)).run_task(task.id)
            self.assertEqual(result.status, TaskStatus.FAILED)
            self.assertFalse(any(event["event_type"] == "tool_started" for event in store.events(task.id)))

    def test_malformed_json_fails_with_diagnostic_and_no_raw_response(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = TaskStore(root / "state.db")
            task = self.make_task(root, store)
            with patch.object(ScriptedClient, "generate", return_value=ModelReply("bad-json-secret", "test")):
                result = LoopController(store, model_registry(), ModelPlanner(ScriptedClient([]))).run_task(task.id)
            self.assertEqual(result.status, TaskStatus.FAILED)
            report = store.events(task.id)[-1]["payload"]["summary"]
            self.assertIn("JSON", report)
            self.assertNotIn("bad-json-secret", report)


class FileReadTests(unittest.TestCase):
    def test_pagination_and_readable_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            (root / "sample.py").write_text("first\nsecond\nthird\n", encoding="utf-8")
            (root / ".env").write_text("secret", encoding="utf-8")
            (Path(directory) / "outside.txt").write_text("external secret", encoding="utf-8")
            registry = model_registry()
            result = registry.execute(ToolCall("file.read", {"path": "sample.py", "start_line": 2, "max_lines": 1}), root)
            self.assertTrue(result.ok)
            self.assertEqual(result.output["content"], "2: second")
            self.assertTrue(result.output["truncated"])
            for path in ("../outside.txt", ".env", str(root / "sample.py"), ".git/config", "private.pem"):
                with self.subTest(path=path):
                    self.assertFalse(registry.execute(ToolCall("file.read", {"path": path}), root).ok)
            for count in (0, 501, "2", True):
                with self.subTest(count=count):
                    self.assertFalse(registry.execute(ToolCall("file.read", {"path": "sample.py", "max_lines": count}), root).ok)

    def test_symlink_escape_and_binary_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            (root / "binary").write_bytes(b"\x00\x01")
            registry = model_registry()
            self.assertFalse(registry.execute(ToolCall("file.read", {"path": "binary"}), root).ok)
            outside = Path(directory) / "outside.txt"
            outside.write_text("external secret", encoding="utf-8")
            try:
                (root / "link.txt").symlink_to(outside)
            except OSError:
                self.skipTest("Symlink creation requires Windows developer mode or elevated rights")
            self.assertFalse(registry.execute(ToolCall("file.read", {"path": "link.txt"}), root).ok)


class StoreMigrationTests(unittest.TestCase):
    def test_existing_database_keeps_tasks_and_adds_model_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "old.db"
            # Create the schema shipped before the model experience and insert an existing task.
            with closing(sqlite3.connect(db)) as connection, connection:
                connection.execute("""CREATE TABLE tasks (
                    id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, instruction TEXT NOT NULL,
                    repository TEXT NOT NULL, workspace TEXT NOT NULL, status TEXT NOT NULL,
                    risk_level TEXT NOT NULL, plan_json TEXT, replan_count INTEGER NOT NULL DEFAULT 0,
                    report_sequence INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                )""")
                connection.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                   ("old-task", "thread", "inspect", directory, directory, "queued", "read_only", None, 0, 0, "now", "now"))
            task = TaskStore(db).get_task("old-task")
            self.assertEqual(task.planner, "local")
            self.assertEqual(task.final_summary, "")
            store = TaskStore(db)
            session = store.create_chat_session(directory)
            self.assertEqual(store.get_chat_session(session.id).workspace, directory)
            self.assertEqual(store.get_task("old-task").instruction, "inspect")


if __name__ == "__main__":
    unittest.main()
