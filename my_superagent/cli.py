from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .chat import ChatController
from .loop import LoopController
from .config import ConfigurationError, ModelConfig
from .llm import ModelAPIError, ModelClient
from .models import ChatTurnStatus, RiskLevel, Task, TaskStatus, new_id
from .model_planner import ModelPlanner
from .planner import LocalPlanner
from .store import TaskStore
from .tools import default_registry, model_registry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="superagent", description="mySuperAgent local prototype")
    subparsers = parser.add_subparsers(dest="command", required=True)

    task = subparsers.add_parser("task", help="create, inspect, or run tasks")
    task.add_argument("value", nargs="?", help="instruction, list, show, run, or cancel")
    task.add_argument("extra", nargs="*", help="task id for show/run/cancel")
    task.add_argument("--repo", default=".", help="repository/workspace path")
    task.add_argument("--db", default=".superagent/state.db", help="SQLite state path")
    add_runtime_options(task)

    run = subparsers.add_parser("run", help="run an existing task")
    run.add_argument("task_id")
    run.add_argument("--db", default=".superagent/state.db")
    add_runtime_options(run)

    model = subparsers.add_parser("model", help="check configured model connectivity")
    model.add_argument("action", choices=["check"])
    model.add_argument("--env-file", default=".env", help="local configuration file")

    chat = subparsers.add_parser("chat", help="start or resume a continuous repository conversation")
    chat.add_argument("--repo", default=None, help="workspace path; new sessions default to the current directory")
    chat.add_argument("--resume", metavar="SESSION_ID", help="resume a saved chat session")
    chat.add_argument("--db", default=".superagent/state.db", help="SQLite state path")
    chat.add_argument("--env-file", default=".env", help="model configuration file")

    return parser


def add_runtime_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--planner", choices=["local", "model"], default=None, help="planner; saved on task creation (default: local)")
    parser.add_argument("--watch", action="store_true", help="show live progress; also execute a newly created task")
    parser.add_argument("--env-file", default=".env", help="model configuration file")


def main(argv: list[str] | None = None) -> int:
    # Keep Chinese progress and model answers readable in Windows pipes and log files.
    if sys.platform == "win32":
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        if args.command == "model":
            return check_model(args.env_file)
        if args.command == "run":
            return run_task(args.task_id, args.db, args.planner, args.watch, args.env_file)
        if args.command == "chat":
            return handle_chat(args)
        return handle_task(args)
    except (ConfigurationError, ModelAPIError, ValueError, OSError) as error:
        print(f"Superagent failed: {error}", file=sys.stderr)
        return 1


def check_model(env_file: str) -> int:
    try:
        config = ModelConfig.load(env_file)
        reply = ModelClient(config).generate("Reply with only OK.")
    except (ConfigurationError, ModelAPIError, OSError) as error:
        print(f"Model check failed: {error}", file=sys.stderr)
        return 1
    print(f"Model check passed: {config.model} via {config.base_url} ({config.api_mode})")
    usage = {
        name: value for name in ("input_tokens", "output_tokens", "total_tokens", "prompt_tokens", "completion_tokens")
        if isinstance(reply.usage, dict) and isinstance(value := reply.usage.get(name), int)
    }
    print(f"Text response received; usage: {json.dumps(usage)}")
    return 0


def handle_task(args: argparse.Namespace) -> int:
    store = TaskStore(args.db)
    value = args.value
    if value == "list":
        for task in store.list_tasks():
            print(f"{task.id}\t{task.status.value}\t{task.instruction}")
        return 0

    if value in {"show", "run", "cancel"}:
        if not args.extra:
            print(f"task {value} requires a task id", file=sys.stderr)
            return 2
        task_id = args.extra[0]
        if value == "show":
            return show_task(store, task_id)
        if value == "run":
            return run_task(task_id, args.db, args.planner, args.watch, args.env_file)
        task = store.get_task(task_id)
        if task is None:
            print(f"Unknown task: {task_id}", file=sys.stderr)
            return 1
        task.status = TaskStatus.CANCELLED
        store.save_task(task)
        store.append_event(task.id, "task_cancelled", {"reason": "cancelled by user"})
        print(f"cancelled {task.id}")
        return 0

    if not value:
        print("task requires an instruction or list/show/run/cancel", file=sys.stderr)
        return 2

    repository = str(Path(args.repo).resolve())
    task = Task(
        id=new_id("task"),
        thread_id=new_id("thread"),
        instruction=value,
        repository=repository,
        workspace=repository,
        risk_level=RiskLevel.READ_ONLY,
        planner=args.planner or "local",
    )
    store.create_task(task)
    store.append_event(task.id, "task_created", {"instruction": value, "repository": repository})
    print(task.id, flush=True)
    if args.watch:
        return run_task(task.id, args.db, watch=True, env_file=args.env_file)
    return 0


def show_task(store: TaskStore, task_id: str) -> int:
    task = store.get_task(task_id)
    if task is None:
        print(f"Unknown task: {task_id}", file=sys.stderr)
        return 1
    print(json.dumps({
        "id": task.id,
        "thread_id": task.thread_id,
        "instruction": task.instruction,
        "repository": task.repository,
        "workspace": task.workspace,
        "status": task.status.value,
        "planner": task.planner,
        "final_summary": task.final_summary,
        "plan": task.plan.to_dict() if task.plan else None,
        "events": store.events(task.id),
    }, ensure_ascii=False, indent=2))
    return 0


def print_progress(event_type: str, payload: dict) -> None:
    if event_type == "model_started":
        action = {"plan": "生成计划", "action": "生成下一步动作", "chat": "思考回复"}.get(payload["phase"], "处理请求")
        print(f"[模型] 正在{action}…", flush=True)
    elif event_type == "plan_created":
        print("[计划]", flush=True)
        for index, step in enumerate(payload["steps"], 1):
            print(f"  {index}. {step['objective']}", flush=True)
    elif event_type == "step_started":
        print(f"[步骤] {payload['objective']}", flush=True)
    elif event_type == "tool_started":
        print(f"[工具] {payload['tool']} {json.dumps(payload.get('arguments', {}), ensure_ascii=False)}", flush=True)
    elif event_type == "tool_finished":
        print(f"[结果] {'成功' if payload['ok'] else '失败'}" + (f"：{payload['error']}" if payload.get("error") else ""), flush=True)
    elif event_type == "context_trimmed":
        print(f"[上下文] 已省略 {payload['omitted_messages']} 条较早记录，完整问答可通过 /history 查看", flush=True)


CHAT_HELP = "命令：/help 查看帮助，/history 查看问答，/retry 重试失败或中断的问题，/exit 保存并退出"


def handle_chat(args: argparse.Namespace) -> int:
    store = TaskStore(args.db)
    client = ModelClient(ModelConfig.load(args.env_file))
    controller = ChatController(store, client, on_event=print_progress)
    session = controller.load_session(args.resume, args.repo) if args.resume else controller.create_session(args.repo or ".")
    print(f"会话：{session.id}\n工作目录：{session.workspace}", flush=True)
    print(CHAT_HELP, flush=True)
    turns = store.chat_turns(session.id)
    if turns:
        latest = turns[-1]
        print(f"最近一轮：{latest.status.value}", flush=True)
        if latest.error:
            print(latest.error, flush=True)
    while True:
        try:
            text = input("你：").strip()
        except (EOFError, KeyboardInterrupt):
            print(flush=True)
            break
        if not text:
            continue
        if text == "/exit":
            break
        if text == "/help":
            print(CHAT_HELP, flush=True)
            continue
        if text == "/history":
            for turn in store.chat_turns(session.id):
                print(f"你：{turn.instruction}", flush=True)
                if turn.status == ChatTurnStatus.COMPLETED:
                    print(f"Agent：{turn.reply}", flush=True)
                else:
                    print(f"[{turn.status.value}] {turn.error}", flush=True)
            continue
        if text.startswith("/") and text != "/retry":
            print("请使用 /help 查看可用命令", flush=True)
            continue
        try:
            turn = controller.retry(session.id) if text == "/retry" else controller.send(session.id, text)
        except ValueError as error:
            print(str(error), flush=True)
            continue
        if turn.status == ChatTurnStatus.COMPLETED:
            print(f"Agent：{turn.reply}", flush=True)
        else:
            print(f"[{turn.status.value}] {turn.error}", flush=True)
    print(f"会话已保存：{session.id}", flush=True)
    command = ["python", "-m", "my_superagent.cli", "chat", "--resume", session.id,
               "--db", str(store.path.resolve()), "--env-file", str(Path(args.env_file).resolve())]
    print("继续对话：" + " ".join('"' + part + '"' if " " in part else part for part in command), flush=True)
    return 0


def run_task(task_id: str, db: str, planner: str | None = None, watch: bool = False, env_file: str = ".env") -> int:
    store = TaskStore(db, on_event=print_progress if watch else None)
    existing = store.get_task(task_id)
    if existing is None:
        print(f"Unknown task: {task_id}", file=sys.stderr)
        return 1
    if planner is not None and planner != existing.planner:
        if existing.plan is not None:
            print("Task already has a plan; create a new task to change planners", file=sys.stderr)
            return 1
        existing.planner = planner
        store.save_task(existing)
    if existing.status in {TaskStatus.COMPLETED, TaskStatus.CANCELLED}:
        print(f"{existing.id}: {existing.status.value}")
        if existing.final_summary:
            print(existing.final_summary)
        return 0 if existing.status == TaskStatus.COMPLETED else 1
    if existing.planner == "model":
        client = ModelClient(ModelConfig.load(env_file))
        selected_planner = ModelPlanner(client, lambda event, payload: store.append_event(task_id, event, payload))
        registry = model_registry()
    else:
        selected_planner = LocalPlanner()
        registry = default_registry()
    controller = LoopController(store, registry, selected_planner)
    task = controller.run_task(task_id)
    print(f"{task.id}: {task.status.value}")
    if task.final_summary:
        print("\n" + task.final_summary)
    if task.status in {TaskStatus.FAILED, TaskStatus.BLOCKED}:
        events = store.events(task_id)
        if events:
            print(events[-1]["payload"].get("summary", ""), file=sys.stderr)
    return 0 if task.status.value == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
