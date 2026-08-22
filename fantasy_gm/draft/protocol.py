from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class DraftEvent:
    raw: str


@dataclass(slots=True)
class AutoDraft(DraftEvent):
    team_id: int
    enabled: bool


@dataclass(slots=True)
class Token(DraftEvent):
    token: str


@dataclass(slots=True)
class Clock(DraftEvent):
    state: int
    milliseconds: int
    team_id: int | None = None


@dataclass(slots=True)
class AutoSuggest(DraftEvent):
    player_id: int


@dataclass(slots=True)
class Joined(DraftEvent):
    team_id: int
    member_id: str


@dataclass(slots=True)
class State(DraftEvent):
    state: int


@dataclass(slots=True)
class Selecting(DraftEvent):
    team_id: int
    milliseconds: int


@dataclass(slots=True)
class Selected(DraftEvent):
    team_id: int
    player_id: int
    selection_type: int
    member_id: str | None = None


@dataclass(slots=True)
class Pong(DraftEvent):
    payload: str


@dataclass(slots=True)
class Unknown(DraftEvent):
    command: str


@dataclass(slots=True)
class Init(DraftEvent):
    payload: str


@dataclass(slots=True)
class Chat(DraftEvent):
    team_id: int
    member_id: str
    timestamp_ms: int
    text: str


def parse_message(message: str) -> DraftEvent:
    raw = message.strip()
    if not raw:
        return Unknown(raw=message, command="")

    parts = raw.split()
    command = parts[0].upper()

    try:
        if command == "AUTODRAFT" and len(parts) >= 3:
            return AutoDraft(
                raw=message,
                team_id=int(parts[1]),
                enabled=parts[2].lower() == "true",
            )

        if command == "TOKEN" and len(parts) >= 2:
            return Token(raw=message, token=" ".join(parts[1:]))

        if command == "CLOCK" and len(parts) >= 3:
            return Clock(
                raw=message,
                state=int(parts[1]),
                milliseconds=int(parts[2]),
                team_id=int(parts[3]) if len(parts) >= 4 else None,
            )

        if command == "AUTOSUGGEST" and len(parts) >= 2:
            return AutoSuggest(raw=message, player_id=int(parts[1]))

        if command == "JOINED" and len(parts) >= 3:
            return Joined(
                raw=message,
                team_id=int(parts[1]),
                member_id=parts[2],
            )

        if command == "STATE" and len(parts) >= 2:
            return State(raw=message, state=int(parts[1]))

        if command == "SELECTING" and len(parts) >= 3:
            return Selecting(
                raw=message,
                team_id=int(parts[1]),
                milliseconds=int(parts[2]),
            )

        if command == "SELECTED" and len(parts) >= 4:
            return Selected(
                raw=message,
                team_id=int(parts[1]),
                player_id=int(parts[2]),
                selection_type=int(parts[3]),
                member_id=parts[4] if len(parts) >= 5 else None,
            )

        if command == "CHAT" and len(parts) >= 5:
            chat_parts = raw.split(" ", 4)

            return Chat(
                raw=message,
                team_id=int(chat_parts[1]),
                member_id=chat_parts[2],
                timestamp_ms=int(chat_parts[3]),
                text=chat_parts[4],
            )

        if command == "PONG":
            return Pong(
                raw=message,
                payload=" ".join(parts[1:]) if len(parts) > 1 else "",
            )

        if command == "INIT":
            return Init(
                raw=message,
                payload=raw.removeprefix("INIT").strip(),
            )

    except (ValueError, IndexError):
        pass

    return Unknown(raw=message, command=command)
