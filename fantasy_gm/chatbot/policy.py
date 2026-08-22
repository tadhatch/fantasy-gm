from __future__ import annotations

import time
from collections import deque

from .models import ChatMessage


class ChatPolicy:
    """
    Hard safety/behavior gates before and after the model.

    The LLM never gets unilateral permission to spam the room.
    """

    def __init__(
        self,
        *,
        team_name: str = "the reservists",
        max_actions_per_minute: int = 3,
        max_replies_per_minute: int = 2,
    ):
        self.team_name = team_name.lower()
        self.max_actions_per_minute = max(1, max_actions_per_minute)
        self.max_replies_per_minute = max(1, max_replies_per_minute)
        self._actions: deque[float] = deque()
        self._replies: deque[float] = deque()

    @staticmethod
    def _prune(queue: deque[float], now: float) -> None:
        cutoff = now - 60.0
        while queue and queue[0] < cutoff:
            queue.popleft()

    def can_consider(self, message: ChatMessage) -> tuple[bool, str]:
        if message.is_self:
            return False, "self"
        if not message.text.strip():
            return False, "empty"

        now = time.monotonic()
        self._prune(self._actions, now)
        if len(self._actions) >= self.max_actions_per_minute:
            return False, "action_rate_limit"

        return True, "ok"

    def can_reply(self) -> bool:
        now = time.monotonic()
        self._prune(self._replies, now)
        return len(self._replies) < self.max_replies_per_minute

    def record_action(self, *, reply: bool) -> None:
        now = time.monotonic()
        self._actions.append(now)
        if reply:
            self._replies.append(now)
