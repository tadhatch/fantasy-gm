from __future__ import annotations

from typing import Protocol

from .models import ChatEvent, ChatReaction


class ChatTransport(Protocol):
    topic_id: str | None

    def connect(self) -> None:
        ...

    def receive(self, timeout: float | None = None) -> ChatEvent | None:
        ...

    def send(self, text: str) -> None:
        ...

    def react(self, message_id: str, reaction: ChatReaction) -> None:
        ...

    def close(self) -> None:
        ...
