from __future__ import annotations

from itertools import count

from .models import ChatMessage, ChatReaction


class LocalChatTransport:
    topic_id = "local"

    def __init__(self):
        self._ids = count(1)

    def connect(self) -> None:
        print("local chatbot ready. type a message; ctrl-c to stop.")

    def receive(self, timeout: float | None = None) -> ChatMessage | None:
        try:
            text = input("chat> ").strip()
        except EOFError:
            return None
        if not text:
            return None
        return ChatMessage(
            id=f"local-{next(self._ids)}",
            author="league friend",
            text=text,
            is_self=False,
        )

    def send(self, text: str) -> None:
        print(f"BOT> {text}")

    def react(self, message_id: str, reaction: ChatReaction) -> None:
        print(f"BOT REACTED {reaction.label} to {message_id}")

    def close(self) -> None:
        pass
