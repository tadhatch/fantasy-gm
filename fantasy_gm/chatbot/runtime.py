from __future__ import annotations

import logging

from fantasy_gm.config import get_settings
from fantasy_gm.espn.client import ESPNClient

from .context import ChatContext
from .espn_transport import EspnChatTransport
from .llm import ChatLLM
from .policy import ChatPolicy
from .service import ChatBotService
from .settings import ChatbotSettings
from .state import DraftStateProvider


def build_chatbot_service(
    *,
    league_id: int | None = None,
    topic_id: str | None = None,
    mode: str | None = None,
) -> ChatBotService:
    app_settings = get_settings()
    chat_settings = ChatbotSettings.from_env(
        topic_id=topic_id,
        mode=mode,
    )

    client = ESPNClient(
        app_settings,
        league_id=league_id,
    )

    state = DraftStateProvider(
        client,
        team_id=app_settings.espn_team_id,
        refresh_seconds=chat_settings.draft_refresh_seconds,
        recent_picks=chat_settings.recent_picks,
    )

    # Do one lightweight state refresh so we can use the actual ESPN team name
    # in deterministic policy metadata. Failure simply leaves the default.
    initial_state = state.refresh(force=True)

    return ChatBotService(
        transport=EspnChatTransport(
            client,
            topic_id=chat_settings.topic_id,
            poll_seconds=chat_settings.poll_seconds,
        ),
        llm=ChatLLM(model=chat_settings.model),
        policy=ChatPolicy(
            team_name=initial_state.our_team_name,
            max_actions_per_minute=chat_settings.max_actions_per_minute,
            max_replies_per_minute=chat_settings.max_replies_per_minute,
        ),
        context=ChatContext(
            max_messages=chat_settings.recent_chat_messages,
        ),
        draft_state=state,
        settings=chat_settings,
    )


def run_chatbot(
    *,
    league_id: int | None = None,
    topic_id: str | None = None,
    mode: str | None = None,
) -> None:
    settings = ChatbotSettings.from_env(topic_id=topic_id, mode=mode)

    logging.basicConfig(
        level=logging.DEBUG if settings.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not settings.enabled:
        logging.getLogger(__name__).info(
            "chatbot disabled by FANTASY_GM_CHATBOT_ENABLED"
        )
        return

    service = build_chatbot_service(
        league_id=league_id,
        topic_id=topic_id,
        mode=mode,
    )
    service.run()
