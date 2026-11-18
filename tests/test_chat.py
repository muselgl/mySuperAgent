from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from my_superagent.chat import ChatController
from my_superagent.cli import main
from my_superagent.config import ModelConfig
from my_superagent.llm import ModelAPIError, ModelReply
from my_superagent.models import ChatTurnStatus
from my_superagent.store import TaskStore


def reply(content: str) -> dict:
    return {"type": "reply", "content": content}


def tool(name: str, **arguments) -> dict:
    return {"type": "call_tool", "tool_call": {"name": name, "arguments": arguments}}


class ScriptedClient:
    def __init__(self, responses: list) -> None:
        self.responses = iter(responses)
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> ModelReply:
        self.prompts.append(prompt)
        value = next(self.responses)
        if isinstance(value, BaseException):
            raise value
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return ModelReply(text, "test-chat", {"input_tokens": 10, "output_tokens": 5})

    def context(self, index: int = -1) -> dict:
        return json.loads(self.prompts[index].split("Conversation context:\n")[1])


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "main.py").write_text('print("saved evidence")\n', encoding="utf-8")
        self.db = self.root / "state.db"
        self.store = TaskStore(self.db)

    def controller(self, responses: list, **options):
        client = ScriptedClient(responses)
        return ChatController(self.store, client, **options), client

    def test_follow_up_uses_previous_answer_without_tool_calls(self):
        controller, client = self.controller([reply("项目入口是 main.py。"), reply("刚才的入口文件是 main.py。")])
        session = controller.create_session(self.repo)
        self.assertEqual(controller.send(session.id, "项目入口是什么？").status, ChatTurnStatus.COMPLETED)
        result = controller.send(session.id, "再说一次刚才的文件名")
        self.assertEqual(result.status, ChatTurnStatus.COMPLETED)
        self.assertEqual(len(client.prompts), 2)
        context = client.context()
        self.assertEqual([item["role"] for item in context["conversation"]], ["user", "assistant", "user"])
        self.assertIn("项目入口是 main.py", json.dumps(context, ensure_ascii=False))
        self.assertFalse(any(item["role"] == "tool" for item in self.store.chat_messages(session.id)))

    def test_tool_error_and_code_evidence_survive_follow_up_and_resume(self):
        controller, client = self.controller([
            tool("file.read", path="missing.py"), tool("file.read", path="main.py"), reply("main.py:1 输出 saved evidence"),
        ])
        session = controller.create_session(self.repo)
        result = controller.send(session.id, "读取入口代码")
        self.assertEqual(result.status, ChatTurnStatus.COMPLETED)
        observations = [item for item in client.context()["conversation"] if item["role"] == "tool"]
        self.assertFalse(observations[0]["payload"]["ok"])
        self.assertEqual(observations[1]["payload"]["output"]["content"], '1: print("saved evidence")')
        fresh_store = TaskStore(self.db)
        fresh_client = ScriptedClient([reply("刚才读取的代码位于 main.py:1")])
        resumed = ChatController(fresh_store, fresh_client)
        self.assertEqual(resumed.load_session(session.id).workspace, str(self.repo.resolve()))
        resumed.send(session.id, "刚才读到什么？")
        self.assertIn("saved evidence", fresh_client.prompts[0])
        messages = fresh_store.chat_messages(session.id)
        self.assertEqual([item["sequence"] for item in messages], list(range(1, len(messages) + 1)))
        usage = [item["payload"] for item in messages if item["payload"]["type"] == "model_finished"]
        self.assertEqual(len(usage), 4)
        self.assertEqual(usage[0]["model"], "test-chat")
        self.assertEqual(usage[0]["usage"]["input_tokens"], 10)

    def test_cli_commands_and_resume_in_a_second_invocation(self):
        first_client = ScriptedClient([reply("首次回答"), reply("连续追问回答")])
        output = io.StringIO()
        with patch("my_superagent.cli.ModelConfig.load", return_value=ModelConfig("test")), \
                patch("my_superagent.cli.ModelClient", return_value=first_client), \
                patch("builtins.input", side_effect=["", "/help", "/unknown", "/retry", "第一问", "接着解释", "/history", "/exit"]), \
                redirect_stdout(output):
            code = main(["chat", "--repo", str(self.repo), "--db", str(self.db)])
        self.assertEqual(code, 0)
        self.assertIn("Agent：连续追问回答", output.getvalue())
        self.assertIn("暂无失败或中断", output.getvalue())
        self.assertIn("继续对话：", output.getvalue())
        self.assertEqual(len(first_client.prompts), 2)
        session_id = output.getvalue().split("会话：", 1)[1].splitlines()[0]
        second_client = ScriptedClient([reply("恢复后的回答")])
        with patch("my_superagent.cli.ModelConfig.load", return_value=ModelConfig("test")), \
                patch("my_superagent.cli.ModelClient", return_value=second_client), \
                patch("builtins.input", side_effect=["继续刚才的话题", EOFError()]), redirect_stdout(io.StringIO()):
            self.assertEqual(main(["chat", "--resume", session_id, "--db", str(self.db)]), 0)
        self.assertIn("第一问", second_client.prompts[0])
        self.assertIn("连续追问回答", second_client.prompts[0])
        self.assertEqual(len(self.store.chat_turns(session_id)), 3)

    def test_idle_ctrl_c_saves_session_and_execution_ctrl_c_returns_to_prompt(self):
        for during_execution in (False, True):
            with self.subTest(during_execution=during_execution):
                client = ScriptedClient([KeyboardInterrupt(), reply("重试成功")])
                inputs = ["分析", "/retry", "/exit"] if during_execution else [KeyboardInterrupt()]
                output = io.StringIO()
                with patch("my_superagent.cli.ModelConfig.load", return_value=ModelConfig("test")), \
                        patch("my_superagent.cli.ModelClient", return_value=client), \
                        patch("builtins.input", side_effect=inputs), redirect_stdout(output):
                    self.assertEqual(main(["chat", "--repo", str(self.repo), "--db", str(self.db)]), 0)
                self.assertIn("会话已保存", output.getvalue())
                if during_execution:
                    self.assertIn("本轮已中断", output.getvalue())
                    self.assertIn("重试成功", output.getvalue())

    def test_resume_marks_crashed_turn_and_preserves_pending_tool_request(self):
        controller, client = self.controller([reply("恢复后的答案")])
        session = controller.create_session(self.repo)
        old = self.store.start_chat_turn(session.id, "读取文件")
        self.store.begin_chat_action(old.id)
        self.store.append_chat_message(old.id, "assistant", {
            "type": "call_tool", "call_id": "pending-call", "tool": "file.read", "arguments": {"path": "main.py"},
        })
        controller.load_session(session.id)
        self.assertEqual(self.store.get_chat_turn(old.id).status, ChatTurnStatus.INTERRUPTED)
        result = controller.retry(session.id)
        self.assertEqual(result.retry_of, old.id)
        self.assertEqual(result.instruction, old.instruction)
        self.assertEqual(result.action_count, 1)
        pending = [item for item in client.context()["conversation"] if item["payload"]["type"] == "pending_tool_call"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["payload"]["status"], "result_not_recorded")
        self.assertEqual(len([item for item in self.store.chat_messages(session.id) if item["payload"]["type"] == "reply"]), 1)

    def test_failed_round_retry_keeps_tool_results_and_starts_new_budget(self):
        controller, client = self.controller([
            tool("file.read", path="main.py"), ModelAPIError("Model request failed: network error"), reply("读取的证据仍在"),
        ])
        session = controller.create_session(self.repo)
        first = controller.send(session.id, "分析代码")
        self.assertEqual(first.status, ChatTurnStatus.FAILED)
        retried = controller.retry(session.id)
        self.assertEqual(retried.status, ChatTurnStatus.COMPLETED)
        self.assertEqual(retried.retry_of, first.id)
        self.assertEqual(retried.action_count, 1)
        self.assertIn("saved evidence", client.prompts[-1])
        self.assertEqual(self.store.get_chat_turn(first.id).status, ChatTurnStatus.FAILED)

    def test_invalid_json_actions_and_reply_are_saved_as_failures(self):
        values = ["invalid-json-private-body", [], tool("shell.run", command="python x.py"), tool([], path="main.py"),
                  {"type": "reply", "content": ""}, {"type": "reply", "content": "x" * 20_001}, {"type": "unknown"}]
        for value in values:
            with self.subTest(value_type=type(value).__name__):
                controller, _ = self.controller([value])
                session = controller.create_session(self.repo)
                turn = controller.send(session.id, "分析")
                self.assertEqual(turn.status, ChatTurnStatus.FAILED)
                self.assertNotIn("private-body", turn.error)
                self.assertFalse(any(item["role"] == "tool" for item in self.store.chat_messages(session.id)))

    def test_decision_budget_blocks_loop_and_allows_reply_on_last_action(self):
        for complete in (False, True):
            with self.subTest(complete=complete):
                actions = [tool("workspace.inspect"), reply("完成")] if complete else [tool("workspace.inspect")] * 2
                controller, _ = self.controller(actions, max_actions=2)
                session = controller.create_session(self.repo)
                turn = controller.send(session.id, "分析")
                self.assertEqual(turn.action_count, 2)
                self.assertEqual(turn.status, ChatTurnStatus.COMPLETED if complete else ChatTurnStatus.BLOCKED)

    def test_timeout_stops_before_executing_returned_tool(self):
        controller, client = self.controller([tool("file.read", path="main.py")])
        session = controller.create_session(self.repo)
        with patch("my_superagent.chat.time.monotonic", side_effect=[0, 0, 1801]):
            turn = controller.send(session.id, "分析")
        self.assertEqual(turn.status, ChatTurnStatus.BLOCKED)
        self.assertEqual(len(client.prompts), 1)
        self.assertFalse(any(item["role"] == "tool" for item in self.store.chat_messages(session.id)))

    def test_context_trimming_preserves_current_question_and_tool_pairs(self):
        controller, _ = self.controller([], context_limit=1800)
        session = controller.create_session(self.repo)
        for index in range(8):
            turn = self.store.start_chat_turn(session.id, f"历史问题{index}" + "q" * 200)
            self.store.finish_chat_turn(turn.id, ChatTurnStatus.COMPLETED, reply="a" * 200)
        tool_turn = self.store.start_chat_turn(session.id, "读取文件")
        self.store.append_chat_message(tool_turn.id, "assistant", {
            "type": "call_tool", "call_id": "kept-pair", "tool": "file.read", "arguments": {"path": "main.py"},
        })
        self.store.append_chat_message(tool_turn.id, "tool", {
            "type": "tool_result", "call_id": "kept-pair", "ok": True, "output": {"content": "e" * 200},
        })
        self.store.finish_chat_turn(tool_turn.id, ChatTurnStatus.COMPLETED, reply="证据已读取")
        current = self.store.start_chat_turn(session.id, "请解释刚才的证据")
        before = len(self.store.chat_messages(session.id))
        context = controller.build_context(session, current, 20)
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), 1800)
        self.assertGreater(context["omitted_messages"], 0)
        conversation = context["conversation"]
        self.assertTrue(any(item["role"] == "user" and item["payload"]["content"] == current.instruction for item in conversation))
        calls = {item["payload"]["call_id"] for item in conversation if item["payload"]["type"] == "call_tool"}
        results = {item["payload"]["call_id"] for item in conversation if item["role"] == "tool"}
        self.assertEqual(calls, {"kept-pair"})
        self.assertEqual(calls, results)
        self.assertEqual(len(self.store.chat_messages(session.id)), before)

    def test_invalid_input_and_workspace_leave_history_intact(self):
        controller, client = self.controller([])
        session = controller.create_session(self.repo)
        for text in ("", "  ", "x" * 16_001):
            with self.subTest(length=len(text)), self.assertRaises(ValueError):
                controller.send(session.id, text)
        self.assertEqual(self.store.chat_turns(session.id), [])
        self.assertEqual(client.prompts, [])
        with self.assertRaisesRegex(ValueError, "saved workspace"):
            controller.load_session(session.id, self.root)
        with self.assertRaisesRegex(ValueError, "Unknown chat session"):
            controller.load_session("chat-missing")
        with self.assertRaises(ValueError):
            controller.create_session(self.root / "missing")
        self.assertEqual(controller.load_session(session.id, self.repo).id, session.id)

    def test_reply_and_completed_status_commit_together(self):
        controller, _ = self.controller([])
        session = controller.create_session(self.repo)
        turn = self.store.start_chat_turn(session.id, "问题")
        append = self.store._append_chat_message

        def fail_after_insert(connection, stored_turn, role, payload):
            append(connection, stored_turn, role, payload)
            raise OSError("simulated write failure")

        with patch.object(self.store, "_append_chat_message", side_effect=fail_after_insert), self.assertRaises(OSError):
            self.store.finish_chat_turn(turn.id, ChatTurnStatus.COMPLETED, reply="回答")
        self.assertEqual(self.store.get_chat_turn(turn.id).status, ChatTurnStatus.RUNNING)
        self.assertEqual([item["role"] for item in self.store.chat_messages(session.id)], ["user"])

    def test_interrupt_after_reply_commit_preserves_completed_answer(self):
        controller, _ = self.controller([])
        session = controller.create_session(self.repo)
        turn = self.store.start_chat_turn(session.id, "问题")
        self.store.finish_chat_turn(turn.id, ChatTurnStatus.COMPLETED, reply="已保存的回答")
        result = self.store.finish_chat_turn(turn.id, ChatTurnStatus.INTERRUPTED, error="late interrupt")
        self.assertEqual(result.status, ChatTurnStatus.COMPLETED)
        self.assertEqual(result.reply, "已保存的回答")
        self.assertEqual([item["role"] for item in self.store.chat_messages(session.id)], ["user", "assistant"])


if __name__ == "__main__":
    unittest.main()
