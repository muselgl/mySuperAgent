"""Versioned event and outbox helpers used by Router and Worker processes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import EventEnvelope, new_id, now_iso


SUPPORTED_SCHEMA_VERSION = 1


def make_event(event_type: str, source: str, payload: dict[str, Any], **context: Any) -> EventEnvelope:
    event = EventEnvelope(event_type=event_type, source=source, payload=payload, **context)
    validate_event(event)
    return event


def validate_event(event: EventEnvelope | dict[str, Any]) -> EventEnvelope:
    value = EventEnvelope.from_dict(event) if isinstance(event, dict) else event
    if value.schema_version != SUPPORTED_SCHEMA_VERSION:
        raise ValueError(f"Unsupported event schema version: {value.schema_version}")
    if not value.event_type or not value.source:
        raise ValueError("event_type and source are required")
    if not isinstance(value.payload, dict):
        raise ValueError("event payload must be an object")
    if value.created_at > now_iso():
        raise ValueError("event created_at must be UTC")
    return value


@dataclass(frozen=True)
class Delivery:
    message_id: str
    event: EventEnvelope
    target: str
    delivery_attempt: int = 1
    ack_type: str = "ack"

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "event_id": self.event.event_id,
            "delivery_attempt": self.delivery_attempt,
            "worker_id": self.target,
            "task_id": self.event.task_id,
            "run_id": self.event.run_id,
            "fencing_token": self.event.fencing_token,
            "payload": self.event.to_dict(),
            "ack_type": self.ack_type,
        }


def delivery_from_dict(value: dict[str, Any]) -> Delivery:
    event_value = value.get("payload", value)
    event = validate_event(event_value)
    return Delivery(
        message_id=str(value.get("message_id") or new_id("msg")),
        event=event,
        target=str(value.get("worker_id") or value.get("target") or ""),
        delivery_attempt=int(value.get("delivery_attempt", 1)),
        ack_type=str(value.get("ack_type", "ack")),
    )
