from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import subprocess
import time
from typing import Any, Callable

from .models import RiskLevel, ToolCall, ToolResult


_EXCLUDED_DIRS = {".git", ".superagent", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache"}


def _visible_path(path: Path, workspace: Path) -> bool:
    try:
        relative = path.resolve().relative_to(workspace)
    except (ValueError, OSError):
        return False
    parts = [part.lower() for part in relative.parts]
    return not any(
        part in _EXCLUDED_DIRS or (part.startswith(".env") and part != ".env.example")
        or part in {".ssh", ".aws", ".azure", ".npmrc", ".pypirc", "credentials", "id_rsa", "id_ed25519"}
        or Path(part).suffix in {".pem", ".key", ".p12", ".pfx"}
        for part in parts
    )


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    permission: RiskLevel
    timeout_seconds: int = 120


class ToolRegistry:
    def __init__(self) -> None:
        self._definitions: dict[str, ToolDefinition] = {}
        self._handlers: dict[str, Callable[[dict[str, Any], Path, ToolDefinition], ToolResult]] = {}

    def register(
        self,
        definition: ToolDefinition,
        handler: Callable[[dict[str, Any], Path, ToolDefinition], ToolResult],
    ) -> None:
        self._definitions[definition.name] = definition
        self._handlers[definition.name] = handler

    def execute(self, call: ToolCall, workspace: str | Path) -> ToolResult:
        definition = self._definitions.get(call.name)
        handler = self._handlers.get(call.name)
        if definition is None or handler is None:
            return ToolResult(ok=False, error=f"Unknown tool: {call.name}")
        return handler(call.arguments, Path(workspace).resolve(), definition)

    def names(self) -> list[str]:
        return sorted(self._definitions)


def _inspect_workspace(arguments: dict[str, Any], workspace: Path, definition: ToolDefinition) -> ToolResult:
    del arguments, definition
    started = time.perf_counter()
    if not workspace.exists():
        return ToolResult(ok=False, error=f"Workspace does not exist: {workspace}")
    files: list[str] = []
    for directory, dirs, filenames in os.walk(workspace, followlinks=False):
        root = Path(directory)
        dirs[:] = sorted(name for name in dirs if _visible_path(root / name, workspace) and not (root / name).is_symlink())
        for name in sorted(filenames):
            path = root / name
            if _visible_path(path, workspace):
                files.append(path.relative_to(workspace).as_posix())
                if len(files) >= 200:
                    break
        if len(files) >= 200:
            break
    return ToolResult(
        ok=True,
        output={"workspace": str(workspace), "file_count_sample": len(files), "files": files},
        duration_ms=int((time.perf_counter() - started) * 1000),
    )


def _read_file(arguments: dict[str, Any], workspace: Path, definition: ToolDefinition) -> ToolResult:
    del definition
    name = arguments.get("path")
    start = arguments.get("start_line", 1)
    count = arguments.get("max_lines", 200)
    if not isinstance(name, str) or not name.strip():
        return ToolResult(ok=False, error="file.read requires a relative path")
    if type(start) is not int or type(count) is not int or start < 1 or not 1 <= count <= 500:
        return ToolResult(ok=False, error="start_line must be positive; max_lines must be between 1 and 500")
    path = workspace / name
    if Path(name).is_absolute() or not _visible_path(path, workspace):
        return ToolResult(ok=False, error="File path is outside the readable workspace scope")
    try:
        with path.open("rb") as source:
            data = source.read(2_000_001)
        if len(data) > 2_000_000:
            return ToolResult(ok=False, error="File exceeds the 2 MB text limit")
        text = data.decode("utf-8-sig")
        if "\x00" in text:
            return ToolResult(ok=False, error="file.read requires a UTF-8 text file")
    except (OSError, UnicodeError, ValueError):
        return ToolResult(ok=False, error="File could not be read as UTF-8 text")
    lines = text.splitlines()
    selected: list[str] = []
    size = 0
    for number, line in enumerate(lines[start - 1:start - 1 + count], start):
        rendered = f"{number}: {line}"
        if size + len(rendered) > 16_000:
            if not selected:
                return ToolResult(ok=False, error="Line exceeds the 16000 character limit")
            break
        selected.append(rendered)
        size += len(rendered) + 1
    return ToolResult(ok=True, output={
        "path": path.relative_to(workspace).as_posix(),
        "start_line": start,
        "end_line": start + len(selected) - 1,
        "total_lines": len(lines),
        "truncated": start - 1 + len(selected) < len(lines),
        "content": "\n".join(selected),
    })


def _git_status(arguments: dict[str, Any], workspace: Path, definition: ToolDefinition) -> ToolResult:
    del arguments
    return _run_command(["git", "status", "--short"], workspace, definition)


def _shell_run(arguments: dict[str, Any], workspace: Path, definition: ToolDefinition) -> ToolResult:
    command = arguments.get("command")
    if not isinstance(command, str) or not command.strip():
        return ToolResult(ok=False, error="shell.run requires a non-empty command")
    try:
        parts = shlex.split(command, posix=os.name != "nt")
    except ValueError as error:
        return ToolResult(ok=False, error=f"Invalid command: {error}")
    if not parts:
        return ToolResult(ok=False, error="shell.run requires a command")
    allowed = {"python", "python3", "py", "pytest", "git", "rg", "node", "npm"}
    executable = Path(parts[0]).name.lower()
    if executable not in allowed:
        return ToolResult(ok=False, error=f"Command is outside the local allowlist: {executable}")
    return _run_command(parts, workspace, definition)


def _run_command(command: list[str], workspace: Path, definition: ToolDefinition) -> ToolResult:
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=definition.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        return ToolResult(
            ok=False,
            error=f"Command timed out after {definition.timeout_seconds}s",
            output={"stdout": error.stdout or "", "stderr": error.stderr or ""},
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
    except OSError as error:
        return ToolResult(ok=False, error=str(error), duration_ms=int((time.perf_counter() - started) * 1000))
    return ToolResult(
        ok=completed.returncode == 0,
        output={"stdout": completed.stdout, "stderr": completed.stderr},
        error=None if completed.returncode == 0 else f"Command exited with {completed.returncode}",
        exit_code=completed.returncode,
        duration_ms=int((time.perf_counter() - started) * 1000),
    )


def default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ToolDefinition("workspace.inspect", RiskLevel.READ_ONLY), _inspect_workspace)
    registry.register(ToolDefinition("git.status", RiskLevel.READ_ONLY), _git_status)
    registry.register(ToolDefinition("shell.run", RiskLevel.WORKSPACE_WRITE), _shell_run)
    registry.register(ToolDefinition("file.read", RiskLevel.READ_ONLY), _read_file)
    return registry


def model_registry() -> ToolRegistry:
    """Tools exposed to the initial model-driven repository analysis experience."""
    registry = ToolRegistry()
    registry.register(ToolDefinition("workspace.inspect", RiskLevel.READ_ONLY), _inspect_workspace)
    registry.register(ToolDefinition("file.read", RiskLevel.READ_ONLY), _read_file)
    registry.register(ToolDefinition("git.status", RiskLevel.READ_ONLY), _git_status)
    return registry
