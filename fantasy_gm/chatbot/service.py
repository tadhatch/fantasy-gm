from __future__ import annotations

import logging
import time

from .context import ChatContext
from .llm import ChatLLM
from .models import ChatMessage, ChatReactionEvent
from .policy import ChatPolicy
from .settings import ChatbotSettings
from .state import DraftStateProvider
from .transport import ChatTransport


logger = logging.getLogger(__name__)


class ChatBotService:
    def __init__(
        self,
        *,
        transport: ChatTransport,
        llm: ChatLLM,
        policy: ChatPolicy,
        context: ChatContext,
        draft_state: DraftStateProvider,
        settings: ChatbotSettings,
    ):
        self.transport = transport
        self.llm = llm
        self.policy = policy
        self.context = context
        self.draft_state = draft_state
        self.settings = settings

    def _trim_reply(self, text: str) -> str:
        text = " ".join(text.strip().split())
        limit = max(40, self.settings.max_message_chars)
        if len(text) <= limit:
            return text
        return text[: limit - 3].rstrip() + "..."

    def _handle_reaction(self, event: ChatReactionEvent) -> None:
        if event.is_self:
            return

        message = self.context.find_message(event.message_id)
        if message is None:
            return

        logger.info(
            "chat reaction %s message=%s user=%s reaction=%s",
            "added" if event.added else "removed",
            event.message_id,
            event.user_id,
            event.reaction.label,
        )
        # Reactions become context via the underlying message snapshot on
        # later polls, but we deliberately do not generate a new LLM reply
        # merely because somebody clicked a reaction.

    def _handle_message(self, message: ChatMessage) -> None:
        self.context.add_message(message)

        allowed, reason = self.policy.can_consider(message)
        logger.info(
            "chat in author=%s self=%s allowed=%s reason=%s text=%r",
            message.author,
            message.is_self,
            allowed,
            reason,
            message.text,
        )
        if not allowed:
            return

        state = self.draft_state.refresh()
        rendered = self.context.render(message, state)

        try:
            action = self.llm.decide(rendered)
        except Exception:
            logger.exception("chat: model decision failed")
            return

        if action.kind == "ignore":
            logger.info("chat ignore reason=%s", action.reason)
            return

        if action.kind == "react":
            if not self.settings.react_enabled or action.reaction is None:
                return

            if self.settings.mode == "shadow":
                logger.info(
                    "chat shadow WOULD REACT message=%s reaction=%s reason=%s",
                    message.id,
                    action.reaction.label,
                    action.reason,
                )
                print(
                    f"[chatbot shadow] WOULD REACT {action.reaction.label} "
                    f"to {message.author}: {message.text}"
                )
                return

            try:
                self.transport.react(message.id, action.reaction)
            except Exception:
                logger.exception("chat: reaction send failed")
                return

            self.policy.record_action(reply=False)
            logger.info(
                "chat reacted message=%s reaction=%s reason=%s",
                message.id,
                action.reaction.label,
                action.reason,
            )
            return

        if action.kind == "reply":
            if not self.policy.can_reply():
                logger.info("chat reply suppressed by reply rate limit")
                return

            text = self._trim_reply(action.text or "")
            if not text:
                return

            if self.settings.mode == "shadow":
                logger.info(
                    "chat shadow WOULD SEND reason=%s text=%r",
                    action.reason,
                    text,
                )
                print(f"[chatbot shadow] WOULD SEND: {text}")
                return

            if self.settings.mode not in {"respond", "full"}:
                logger.warning("chat: unsupported mode=%s", self.settings.mode)
                return

            try:
                self.transport.send(text)
            except Exception:
                logger.exception("chat: message send failed")
                return

            self.policy.record_action(reply=True)
            logger.info("chat sent reason=%s text=%r", action.reason, text)

    def run(self) -> None:
        logger.info(
            "chatbot starting mode=%s model=%s topic_override=%s",
            self.settings.mode,
            self.settings.model,
            self.settings.topic_id or "auto",
        )

        self.transport.connect()

        try:
            # Warm public draft state, but failure is non-fatal.
            self.draft_state.refresh(force=True)

            while True:
                try:
                    event = self.transport.receive(timeout=10.0)
                except KeyboardInterrupt:
                    raise
                except Exception:
                    logger.exception("chat: receive failed; retrying")
                    time.sleep(3.0)
                    continue

                if event is None:
                    continue

                if isinstance(event, ChatMessage):
                    self._handle_message(event)
                else:
                    self._handle_reaction(event)
        finally:
            self.transport.close()
