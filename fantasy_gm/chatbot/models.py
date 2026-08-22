from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from typing import Literal


class ChatReaction(IntEnum):
    FUNNY = 1
    HEART = 2
    SHOCK = 3
    TROPHY = 4
    ANGER = 5
    FIRE = 6
    DISLIKE = 7
    LIKE = 8

    @classmethod
    def from_name(cls, value: str | None) -> "ChatReaction | None":
        if not value:
            return None
        key = value.strip().upper()
        aliases = {
            "LOL": "FUNNY",
            "LAUGH": "FUNNY",
            "LOVE": "HEART",
            "WOW": "SHOCK",
            "CHAMP": "TROPHY",
            "MAD": "ANGER",
            "THUMBS_DOWN": "DISLIKE",
            "THUMBSDOWN": "DISLIKE",
            "THUMBS_UP": "LIKE",
            "THUMBSUP": "LIKE",
        }
        key = aliases.get(key, key)
        try:
            return cls[key]
        except KeyError:
            return None

    @property
    def label(self) -> str:
        return {
            self.FUNNY: "funny",
            self.HEART: "heart",
            self.SHOCK: "shock",
            self.TROPHY: "trophy",
            self.ANGER: "anger",
            self.FIRE: "fire",
            self.DISLIKE: "dislike",
            self.LIKE: "like",
        }[self]


@dataclass(slots=True)
class ChatMessage:
    id: str
    text: str
    author: str
    user_id: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    is_self: bool = False
    reactions: dict[ChatReaction, list[str]] = field(default_factory=dict)


@dataclass(slots=True)
class ChatReactionEvent:
    message_id: str
    reaction: ChatReaction
    user_id: str
    added: bool
    is_self: bool = False


ChatEvent = ChatMessage | ChatReactionEvent


@dataclass(slots=True)
class DraftPickContext:
    overall_pick: int
    round_number: int
    pick_in_round: int
    team_id: int
    player_id: int
    player_name: str


@dataclass(slots=True)
class ChatbotDraftState:
    our_team_id: int
    our_team_name: str = "the reservists"
    current_overall_pick: int | None = None
    current_round: int | None = None
    current_team_id: int | None = None
    our_roster: list[str] = field(default_factory=list)
    recent_picks: list[DraftPickContext] = field(default_factory=list)


@dataclass(slots=True)
class ChatAction:
    kind: Literal["ignore", "reply", "react"]
    reason: str
    text: str | None = None
    reaction: ChatReaction | None = None
