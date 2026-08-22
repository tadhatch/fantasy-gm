from __future__ import annotations

import logging

from fantasy_gm.config import get_settings
from fantasy_gm.espn.client import ESPNClient

from .context import ChatContext
from .llm import ChatLLM
from .local_transport import LocalChatTransport
from .policy import ChatPolicy
from .service import ChatBotService
from .settings import ChatbotSettings
from .state import DraftStateProvider


def run_local_chatbot(*, mode: str = "respond") -> None:
    logging.basicConfig(level=logging.INFO)

    app_settings = get_settings()
    chat_settings = ChatbotSettings.from_env(mode=mode)
    client = ESPNClient(app_settings)

    state = DraftStateProvider(
        client,
        team_id=app_settings.espn_team_id,
        refresh_seconds=chat_settings.draft_refresh_seconds,
        recent_picks=chat_settings.recent_picks,
    )

    service = ChatBotService(
        transport=LocalChatTransport(),
        llm=ChatLLM(model=chat_settings.model),
        policy=ChatPolicy(
            max_actions_per_minute=chat_settings.max_actions_per_minute,
            max_replies_per_minute=chat_settings.max_replies_per_minute,
        ),
        context=ChatContext(
            max_messages=chat_settings.recent_chat_messages,
        ),
        draft_state=state,
        settings=chat_settings,
    )
    service.run()
