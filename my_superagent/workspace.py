"""Task workspace creation and environment lock facade."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import subprocess

from .models import new_id
from .store import TaskStore


@dataclass
class Workspace:
    environment_id: str
    path: Path
    strategy: str
    repository: str
    branch: str | None = None


class WorkspaceManager:
    def __init__(self, store: TaskStore) -> None:
        self.store = store

    def prepare(self, task_id: str, repository: str | Path, *, root: str | Path | None = None, branch: str | None = None) -> Workspace:
        repo = Path(repository).resolve()
        destination_root = Path(root).resolve() if root else repo.parent / ".superagent" / "workspaces"
        destination_root.mkdir(parents=True, exist_ok=True)
        env_id = new_id("env")
        destination = destination_root / task_id
        is_git = (repo / ".git").exists()
        strategy = "worktree" if is_git else "copy"
        if is_git:
            branch = branch or f"superagent/{task_id}"
            if not destination.exists():
                result = subprocess.run(["git", "-C", str(repo), "worktree", "add", "-b", branch, str(destination)], capture_output=True, text=True)
                if result.returncode:
                    strategy = "copy"
        if strategy == "copy" and not destination.exists():
            shutil.copytree(repo, destination, ignore=shutil.ignore_patterns(".git", ".superagent", "__pycache__"))
        with self.store.connection() as connection:
            connection.execute("INSERT INTO environments(id, task_id, path, repository, metadata, created_at) VALUES (?, ?, ?, ?, ?, ?)", (env_id, task_id, str(destination), str(repo), json.dumps({"strategy": strategy, "branch": branch}), self._now()))
        return Workspace(env_id, destination, strategy, str(repo), branch)

    def lock(self, workspace: Workspace, owner: str, *, mode: str = "write", run_id: str | None = None, fencing_token: int = 0) -> str | None:
        return self.store.acquire_lock(workspace.environment_id, owner, mode=mode, run_id=run_id, fencing_token=fencing_token)

    create = prepare

    def release(self, lock_id: str, reason: str = "released") -> bool:
        return self.store.release_lock(lock_id, reason)

    @staticmethod
    def _now() -> str:
        from .models import now_iso
        return now_iso()
