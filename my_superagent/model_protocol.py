from __future__ import annotations

import json
from typing import Any, Callable

from .llm import ModelClient
from .models import ToolCall


class ModelOutputError(ValueError):
    """A model protocol diagnostic that omits the raw response."""


TOOLS = {
    "workspace.inspect": {"description": "List up to 200 workspace files", "arguments": {}},
    "file.read": {
        "description": "Read UTF-8 text with line numbers; use relative paths and paginate large files",
        "arguments": {"path": "required string", "start_line": "optional integer >= 1", "max_lines": "optional integer 1..500, default 200"},
    },
    "git.status": {"description": "Read short Git status", "arguments": {}},
}


def generate_json(
    client: ModelClient, prompt: str, phase: str,
    on_event: Callable[[str, dict], None] | None = None,
) -> dict[str, Any]:
    if on_event:
        on_event("model_started", {"phase": phase})
    reply = client.generate(prompt)
    if on_event:
        usage = {name: value for name, value in reply.usage.items() if type(value) is int} if isinstance(reply.usage, dict) else {}
        on_event("model_finished", {"phase": phase, "model": reply.model, "usage": usage})
    text = reply.text.strip()
    if text.startswith("```") and text.endswith("```"):
        text = "\n".join(text.splitlines()[1:-1])
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        raise ModelOutputError("Model response requires a single JSON object") from None
    if not isinstance(value, dict):
        raise ModelOutputError("Model response requires a JSON object")
    return value


def parse_tool_call(value: Any) -> ToolCall:
    if (not isinstance(value, dict) or not isinstance(value.get("name"), str)
            or value["name"] not in TOOLS or not isinstance(value.get("arguments", {}), dict)):
        raise ModelOutputError("Model selected an invalid tool or arguments")
    return ToolCall(value["name"], value.get("arguments", {}))
