"""A tiny synchronous event bus for live progress updates.

Every interesting thing the agent does is published here. The CLI subscribes
and renders a live console view; the FastAPI server subscribes and re-publishes
the same events as Server-Sent Events. Nothing in the agent core knows or cares
which front-end is attached.
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Literal

EventType = Literal[
    "run_started",
    "planning_started",
    "plan_ready",
    "llm_turn_started",
    "assistant_text",
    "tool_started",
    "tool_succeeded",
    "tool_failed",
    "tool_retrying",
    "run_finished",
    "run_failed",
]


@dataclass(slots=True)
class Event:
    type: EventType
    message: str
    data: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


Subscriber = Callable[[Event], None]


class EventBus:
    """Fan-out publisher. Subscribers must not raise; failures are swallowed
    so a broken renderer can never take down a running agent."""

    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []
        self._history: list[Event] = []
        self._lock = threading.Lock()

    def subscribe(self, subscriber: Subscriber) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(subscriber)

        def unsubscribe() -> None:
            with self._lock:
                if subscriber in self._subscribers:
                    self._subscribers.remove(subscriber)

        return unsubscribe

    def emit(self, type_: EventType, message: str, **data) -> Event:
        event = Event(type=type_, message=message, data=data)
        with self._lock:
            self._history.append(event)
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber(event)
            except Exception:  # pragma: no cover - a renderer must never break a run
                pass
        return event

    @property
    def history(self) -> list[Event]:
        with self._lock:
            return list(self._history)
