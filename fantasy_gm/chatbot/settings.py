from __future__ import annotations

import os
from dataclasses import dataclass


TRUE_VALUES = {"1", "true", "yes", "on"}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in TRUE_VALUES


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return float(raw)


@dataclass(frozen=True, slots=True)
class ChatbotSettings:
    enabled: bool = True
    mode: str = "shadow"
    model: str = "gpt-5.6-luna"
    topic_id: str | None = None
    poll_seconds: float = 3.0
    draft_refresh_seconds: float = 15.0
    max_actions_per_minute: int = 3
    max_replies_per_minute: int = 2
    max_message_chars: int = 280
    recent_chat_messages: int = 14
    recent_picks: int = 12
    proactive: bool = False
    react_enabled: bool = True
    debug: bool = False

    @classmethod
    def from_env(
        cls,
        *,
        topic_id: str | None = None,
        mode: str | None = None,
    ) -> "ChatbotSettings":
        return cls(
            enabled=_env_bool("FANTASY_GM_CHATBOT_ENABLED", True),
            mode=(mode or os.getenv("FANTASY_GM_CHATBOT_MODE", "shadow")).strip().lower(),
            model=os.getenv("FANTASY_GM_CHATBOT_MODEL", "gpt-5.6-luna").strip(),
            topic_id=topic_id or os.getenv("FANTASY_GM_CHAT_TOPIC_ID") or None,
            poll_seconds=_env_float("FANTASY_GM_CHAT_POLL_SECONDS", 3.0),
            draft_refresh_seconds=_env_float(
                "FANTASY_GM_CHAT_DRAFT_REFRESH_SECONDS", 15.0
            ),
            max_actions_per_minute=_env_int(
                "FANTASY_GM_CHAT_MAX_ACTIONS_PER_MINUTE", 3
            ),
            max_replies_per_minute=_env_int(
                "FANTASY_GM_CHAT_MAX_REPLIES_PER_MINUTE", 2
            ),
            max_message_chars=_env_int("FANTASY_GM_CHAT_MAX_CHARS", 280),
            recent_chat_messages=_env_int(
                "FANTASY_GM_CHAT_RECENT_MESSAGES", 14
            ),
            recent_picks=_env_int("FANTASY_GM_CHAT_RECENT_PICKS", 12),
            proactive=_env_bool("FANTASY_GM_CHAT_PROACTIVE", False),
            react_enabled=_env_bool("FANTASY_GM_CHAT_REACTIONS", True),
            debug=_env_bool("FANTASY_GM_CHAT_DEBUG", False),
        )
