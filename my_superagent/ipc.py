"""Bounded in-memory IPC with explicit acknowledgement and dead letters."""
from __future__ import annotations

from dataclasses import dataclass
from queue import Empty, Full, Queue
from threading import Lock
from typing import Any

from .events import Delivery, delivery_from_dict
from .models import EventEnvelope, new_id
from .store import TaskStore


@dataclass(frozen=True)
class Ack:
    message_id: str
    ack_type: str = "ack"
    error: str | None = None


class Broker:
    def __init__(self, store: TaskStore | None = None, capacity: int = 100, max_delivery_attempts: int = 5) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self.store = store
        self.capacity = capacity
        self.max_delivery_attempts = max_delivery_attempts
        self._channels: dict[str, Queue[dict[str, Any]]] = {}
        self._seen: set[str] = set()
        self._dead_letters: list[dict[str, Any]] = []
        self._lock = Lock()

    def register(self, worker_id: str) -> None:
        with self._lock:
            self._channels.setdefault(worker_id, Queue(self.capacity))

    def unregister(self, worker_id: str) -> None:
        with self._lock:
            self._channels.pop(worker_id, None)

    def publish(self, event: EventEnvelope, worker_id: str, *, block: bool = False) -> bool:
        if worker_id not in self._channels:
            self.register(worker_id)
        message = {"message_id": new_id("msg"), "worker_id": worker_id, "payload": event.to_dict(), "delivery_attempt": 1, "ack_type": "ack"}
        try:
            self._channels[worker_id].put(message, block=block)
            return True
        except Full:
            if self.store:
                self.store.enqueue_outbox(event.event_id, worker_id, message)
            return False

    def receive(self, worker_id: str, timeout: float | None = None) -> Delivery | None:
        channel = self._channels.get(worker_id)
        if channel is None:
            return None
        try:
            message = channel.get(timeout=timeout)
        except Empty:
            return None
        try:
            return delivery_from_dict(message)
        except (TypeError, ValueError, KeyError) as error:
            self._dead_letters.append({"message": message, "error": str(error)})
            return None

    def ack(self, delivery: Delivery, *, success: bool = True, error: str | None = None) -> Ack:
        with self._lock:
            if delivery.event.event_id in self._seen:
                return Ack(delivery.message_id, "duplicate")
            if success:
                self._seen.add(delivery.event.event_id)
                return Ack(delivery.message_id)
            if delivery.delivery_attempt >= self.max_delivery_attempts:
                self._dead_letters.append({"message_id": delivery.message_id, "event_id": delivery.event.event_id, "error": error or "delivery failed"})
                return Ack(delivery.message_id, "dead_letter", error)
            return Ack(delivery.message_id, "retry", error)

    @property
    def dead_letters(self) -> list[dict[str, Any]]:
        return list(self._dead_letters)

    def pending(self, worker_id: str) -> int:
        channel = self._channels.get(worker_id)
        return channel.qsize() if channel else 0


MemoryBroker = Broker
