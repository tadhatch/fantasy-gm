from __future__ import annotations

import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

import requests

from fantasy_gm.espn.client import ESPNAuthError, ESPNClient, ESPNError

from .models import ChatEvent, ChatMessage, ChatReaction, ChatReactionEvent


logger = logging.getLogger(__name__)


class EspnChatTransport:
    """
    ESPN fantasy league chat transport reconstructed from the browser HAR.

    Known protocol:
      discover:
        GET /communication/?view=chat_conversations&platform=chat

      read:
        GET /communication/topics/{topic_id}
            ?view=chat_conversation&platform=chat

      append:
        POST /communication/topics/{topic_id}/messages/
             ?source={member_id}&platform=chat

      react:
        PUT /communication/topics/{topic_id}/messages/{message_id}/reactions/{reaction_id}
            ?platform=chat

    A topic ID can be forced with --topic-id or FANTASY_GM_CHAT_TOPIC_ID.
    """

    BASE = "https://lm-api-communication.fantasy.espn.com/apis/v3/games/ffl"

    def __init__(
        self,
        client: ESPNClient,
        *,
        topic_id: str | None = None,
        poll_seconds: float = 3.0,
        bootstrap_existing: bool = True,
    ):
        self.client = client
        self.member_id = client.settings.espn_swid
        self.poll_seconds = max(0.5, poll_seconds)
        self.topic_id = topic_id
        self.bootstrap_existing = bootstrap_existing

        self._seen_message_ids: set[str] = set()
        self._reaction_snapshot: dict[str, dict[int, set[str]]] = {}
        self._pending: deque[ChatEvent] = deque()
        self._member_names: dict[str, str] = {}
        self._connected = False

    @property
    def communication_url(self) -> str:
        return (
            f"{self.BASE}/seasons/{self.client.settings.espn_season}"
            f"/segments/0/leagues/{self.client.league_id}/communication"
        )

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: Any = None,
        json_body: Any = None,
    ) -> Any:
        try:
            response = self.client.session.request(
                method,
                url,
                params=params,
                json=json_body,
                timeout=20,
            )
        except requests.RequestException as exc:
            raise ESPNError(f"ESPN chat request failed: {exc}") from exc

        if response.status_code in (401, 403):
            raise ESPNAuthError(
                f"ESPN chat authentication failed ({response.status_code})"
            )

        response.raise_for_status()

        if not response.content:
            return {}

        try:
            return response.json()
        except requests.JSONDecodeError as exc:
            raise ESPNError(
                f"ESPN chat returned non-JSON response from {response.url}"
            ) from exc

    def _load_member_names(self) -> None:
        try:
            data = self.client.get_league(["mTeam"])
        except Exception:
            logger.exception("chat: could not load ESPN member names")
            return

        for member in data.get("members", []) or []:
            member_id = member.get("id")
            if not member_id:
                continue
            name = (
                member.get("displayName")
                or member.get("firstName")
                or member.get("lastName")
                or member_id
            )
            self._member_names[member_id] = str(name)

    def _discover_topic(self) -> str:
        data = self._request_json(
            "GET",
            f"{self.communication_url}/",
            params={
                "view": "chat_conversations",
                "platform": "chat",
            },
        )

        topics = data.get("topics", []) if isinstance(data, dict) else []
        chat_topics = [
            topic
            for topic in topics
            if topic.get("type") == "CHAT_ALL_MEMBERS"
        ]

        if not chat_topics:
            raise ESPNError(
                "No ESPN CHAT_ALL_MEMBERS topic found. "
                "Open league chat once in the browser or set "
                "FANTASY_GM_CHAT_TOPIC_ID / --topic-id."
            )

        # Prefer the topic with the most recent activity when ESPN returns several.
        chat_topics.sort(
            key=lambda topic: (
                topic.get("lastMessageDate")
                or topic.get("date")
                or 0
            ),
            reverse=True,
        )
        return str(chat_topics[0]["id"])

    def _get_conversation(self) -> dict[str, Any]:
        if not self.topic_id:
            raise ESPNError("ESPN chat topic is not connected")

        data = self._request_json(
            "GET",
            f"{self.communication_url}/topics/{self.topic_id}",
            params={
                "view": "chat_conversation",
                "platform": "chat",
            },
        )
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _parse_reactions(raw: dict[str, Any]) -> dict[ChatReaction, list[str]]:
        parsed: dict[ChatReaction, list[str]] = {}
        for reaction_id, users in (raw or {}).items():
            try:
                reaction = ChatReaction(int(reaction_id))
            except (TypeError, ValueError):
                continue
            parsed[reaction] = [str(user) for user in (users or [])]
        return parsed

    def _normalize_message(self, raw: dict[str, Any]) -> ChatMessage:
        user_id = raw.get("author")
        date_ms = raw.get("date")
        if isinstance(date_ms, (int, float)):
            timestamp = datetime.fromtimestamp(date_ms / 1000, tz=timezone.utc)
        else:
            timestamp = datetime.now(timezone.utc)

        return ChatMessage(
            id=str(raw["id"]),
            text=str(raw.get("content") or ""),
            author=self._member_names.get(str(user_id), str(user_id or "unknown")),
            user_id=str(user_id) if user_id is not None else None,
            timestamp=timestamp,
            is_self=str(user_id) == str(self.member_id),
            reactions=self._parse_reactions(raw.get("reactions") or {}),
        )

    @staticmethod
    def _reaction_sets(
        raw: dict[str, Any],
    ) -> dict[int, set[str]]:
        result: dict[int, set[str]] = {}
        for reaction_id, users in (raw or {}).items():
            try:
                rid = int(reaction_id)
            except (TypeError, ValueError):
                continue
            result[rid] = {str(user) for user in (users or [])}
        return result

    def _queue_reaction_diffs(
        self,
        message_id: str,
        new_snapshot: dict[int, set[str]],
    ) -> None:
        old_snapshot = self._reaction_snapshot.get(message_id, {})

        all_ids = set(old_snapshot) | set(new_snapshot)
        for reaction_id in all_ids:
            try:
                reaction = ChatReaction(reaction_id)
            except ValueError:
                continue

            old_users = old_snapshot.get(reaction_id, set())
            new_users = new_snapshot.get(reaction_id, set())

            for user_id in sorted(new_users - old_users):
                self._pending.append(
                    ChatReactionEvent(
                        message_id=message_id,
                        reaction=reaction,
                        user_id=user_id,
                        added=True,
                        is_self=user_id == str(self.member_id),
                    )
                )

            for user_id in sorted(old_users - new_users):
                self._pending.append(
                    ChatReactionEvent(
                        message_id=message_id,
                        reaction=reaction,
                        user_id=user_id,
                        added=False,
                        is_self=user_id == str(self.member_id),
                    )
                )

        self._reaction_snapshot[message_id] = new_snapshot

    def _ingest_conversation(
        self,
        conversation: dict[str, Any],
        *,
        emit_messages: bool,
        emit_reactions: bool,
    ) -> None:
        messages = conversation.get("messages", []) or []
        messages = sorted(
            messages,
            key=lambda raw: (raw.get("date") or 0, str(raw.get("id") or "")),
        )

        for raw in messages:
            message_id = str(raw.get("id") or "")
            if not message_id:
                continue

            reaction_snapshot = self._reaction_sets(raw.get("reactions") or {})

            if emit_reactions and message_id in self._reaction_snapshot:
                self._queue_reaction_diffs(message_id, reaction_snapshot)
            else:
                self._reaction_snapshot[message_id] = reaction_snapshot

            if message_id not in self._seen_message_ids:
                self._seen_message_ids.add(message_id)
                if emit_messages:
                    self._pending.append(self._normalize_message(raw))

    def connect(self) -> None:
        self._load_member_names()

        if self.topic_id:
            logger.info("chat: using forced topic id=%s", self.topic_id)
        else:
            self.topic_id = self._discover_topic()
            logger.info(
                "chat: discovered CHAT_ALL_MEMBERS topic id=%s",
                self.topic_id,
            )

        conversation = self._get_conversation()
        self._ingest_conversation(
            conversation,
            emit_messages=not self.bootstrap_existing,
            emit_reactions=False,
        )

        logger.info(
            "chat: connected league=%s topic=%s existing_messages=%s",
            self.client.league_id,
            self.topic_id,
            len(self._seen_message_ids),
        )
        self._connected = True

    def receive(self, timeout: float | None = None) -> ChatEvent | None:
        if not self._connected:
            raise ESPNError("ESPN chat transport is not connected")

        if self._pending:
            return self._pending.popleft()

        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)

        while True:
            conversation = self._get_conversation()
            self._ingest_conversation(
                conversation,
                emit_messages=True,
                emit_reactions=True,
            )

            if self._pending:
                return self._pending.popleft()

            if deadline is not None and time.monotonic() >= deadline:
                return None

            sleep_for = self.poll_seconds
            if deadline is not None:
                sleep_for = min(
                    sleep_for,
                    max(0.0, deadline - time.monotonic()),
                )

            if sleep_for <= 0:
                return None
            time.sleep(sleep_for)

    def send(self, text: str) -> None:
        if not self.topic_id:
            raise ESPNError("ESPN chat topic is not connected")

        payload = {
            "author": self.member_id,
            "content": text,
            "messageTypeId": "",
        }

        data = self._request_json(
            "POST",
            f"{self.communication_url}/topics/{self.topic_id}/messages/",
            params={
                "source": self.member_id,
                "platform": "chat",
            },
            json_body=payload,
        )

        # The captured endpoint returns a list containing the new message.
        if isinstance(data, list):
            for raw in data:
                if not isinstance(raw, dict) or not raw.get("id"):
                    continue
                message_id = str(raw["id"])
                self._seen_message_ids.add(message_id)
                self._reaction_snapshot[message_id] = self._reaction_sets(
                    raw.get("reactions") or {}
                )

    def react(self, message_id: str, reaction: ChatReaction) -> None:
        if not self.topic_id:
            raise ESPNError("ESPN chat topic is not connected")

        self._request_json(
            "PUT",
            (
                f"{self.communication_url}/topics/{self.topic_id}"
                f"/messages/{message_id}/reactions/{int(reaction)}"
            ),
            params={"platform": "chat"},
        )

    def close(self) -> None:
        self._connected = False
