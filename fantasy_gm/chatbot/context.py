from __future__ import annotations

from collections import deque

from .models import ChatMessage, ChatbotDraftState


class ChatContext:
    def __init__(self, *, max_messages: int = 14):
        self.recent_chat: deque[ChatMessage] = deque(maxlen=max(4, max_messages))

    def add_message(self, message: ChatMessage) -> None:
        self.recent_chat.append(message)

    def find_message(self, message_id: str) -> ChatMessage | None:
        for message in reversed(self.recent_chat):
            if message.id == message_id:
                return message
        return None

    def render(
        self,
        incoming: ChatMessage,
        draft_state: ChatbotDraftState,
    ) -> str:
        lines: list[str] = [
            "PUBLIC DRAFT CONTEXT",
            f"our team: {draft_state.our_team_name}",
        ]

        if draft_state.current_round is not None:
            lines.append(f"current round: {draft_state.current_round}")
        if draft_state.current_overall_pick is not None:
            lines.append(
                f"current overall pick: {draft_state.current_overall_pick}"
            )
        if draft_state.current_team_id is not None:
            lines.append(f"team currently on clock: {draft_state.current_team_id}")

        if draft_state.our_roster:
            lines.extend(["", "our roster:"])
            lines.extend(f"- {name}" for name in draft_state.our_roster)

        if draft_state.recent_picks:
            lines.extend(["", "recent draft picks:"])
            for pick in draft_state.recent_picks:
                lines.append(
                    f"- #{pick.overall_pick} r{pick.round_number}."
                    f"{pick.pick_in_round}: {pick.player_name} "
                    f"(team {pick.team_id})"
                )

        if draft_state.notable_player_news:
            lines.extend(["", "notable recent player news:"])
            for item in draft_state.notable_player_news:
                lines.append(
                    f"- {item.player_name} "
                    f"({item.category}, impact {item.direct_delta:+.1f}): "
                    f"{item.summary}"
                )

        if self.recent_chat:
            lines.extend(["", "recent league chat:"])
            for message in self.recent_chat:
                who = "us" if message.is_self else message.author
                lines.append(f"- {who}: {message.text}")

        lines.extend(
            [
                "",
                "new message:",
                f"{incoming.author}: {incoming.text}",
            ]
        )
        return "\n".join(lines)
