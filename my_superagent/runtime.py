"""Small service runtimes for CLI-operated Router, Worker, Supervisor and Watchdog."""
from __future__ import annotations

import argparse
import signal
import time
from dataclasses import dataclass

from .ipc import Broker
from .loop import LoopController
from .store import TaskStore


@dataclass
class Service:
    name: str
    store: TaskStore
    interval: float = 0.25
    running: bool = False

    def serve(self, *, once: bool = False, duration: float | None = None) -> int:
        self.running = True
        started = time.monotonic()
        while self.running:
            self.tick()
            if once or (duration is not None and time.monotonic() - started >= duration):
                break
            time.sleep(self.interval)
        self.running = False
        return 0

    def stop(self, *_: object) -> None:
        self.running = False

    def tick(self) -> None:
        return None


class RouterService(Service):
    def __init__(self, store: TaskStore, broker: Broker | None = None) -> None:
        super().__init__("router", store)
        self.broker = broker or Broker(store)

    def submit(self, task_id: str) -> bool:
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        return True


class WorkerService(Service):
    def __init__(self, store: TaskStore, worker_id: str, loop: LoopController | None = None) -> None:
        super().__init__("worker", store)
        self.worker_id = worker_id
        self.loop = loop

    def tick(self) -> None:
        if self.loop is None:
            return
        lease = self.store.claim_task(self.worker_id)
        if lease:
            try:
                self.loop.run_task(lease.task_id)
            finally:
                self.store.release_lease(lease)


class WatchdogService(Service):
    def tick(self) -> None:
        now = time.time()
        with self.store.connection() as connection:
            rows = connection.execute("SELECT id, lease_expires_at FROM tasks WHERE status='running' AND active_run_id IS NOT NULL").fetchall()
            for row in rows:
                if row["lease_expires_at"]:
                    from datetime import datetime
                    try:
                        expired = datetime.fromisoformat(row["lease_expires_at"]).timestamp() <= now
                    except ValueError:
                        expired = True
                    if expired:
                        connection.execute("UPDATE tasks SET status='queued', active_run_id=NULL, lease_owner=NULL, lease_expires_at=NULL, recovery_count=recovery_count+1, updated_at=? WHERE id=? AND status='running'", (time.strftime('%Y-%m-%dT%H:%M:%S+00:00'), row["id"]))


class SupervisorService(Service):
    pass
