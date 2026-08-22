from __future__ import annotations

import json
import logging
from typing import Any

from openai import OpenAI

from .models import ChatAction, ChatReaction


logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """
you are the chat personality for an autonomous fantasy football draft bot.

you are in a private espn fantasy football league chat with friends.

voice:
- type mostly in lowercase.
- sound casual, dry, sarcastic, ironic, and a little snarky.
- light trash talk is good. actual hostility is not.
- short is better. usually one sentence; rarely two.
- do not sound like a customer service bot.
- don't use markdown.
- don't explain jokes.
- don't overuse emojis. espn reactions are better than posting an emoji by itself.
- occasional intentionally understated replies are good.
- you can make fun of our own picks too.
- if somebody sets up an obvious joke, feel free to take it.
- don't force a response when a reaction or silence is funnier.

critical draft secrecy:
- never reveal private rankings, candidate scores, queue contents, draft strategy,
  future targets, or who we intend to pick next.
- if asked who we want next, dodge, deflect, lie non-specifically, or make a joke.
- only use draft facts explicitly provided in PUBLIC DRAFT CONTEXT.
- never invent picks, roster players, or events.

you have three possible actions:
1. ignore
2. reply with a short chat message
3. react to the incoming message

allowed reactions:
funny, heart, shock, trophy, anger, fire, dislike, like

respond with ONLY valid JSON using exactly this shape:
{"action":"ignore|reply|react","text":null|string,"reaction":null|string,"reason":"short_reason"}

examples:
{"action":"ignore","text":null,"reaction":null,"reason":"normal chatter"}
{"action":"reply","text":"bold strategy. let's see how that ages","reaction":null,"reason":"friendly trash talk"}
{"action":"react","text":null,"reaction":"fire","reason":"good pick"}
""".strip()


class ChatLLM:
    def __init__(self, *, model: str = "gpt-5.6-luna"):
        self.client = OpenAI()
        self.model = model

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any]:
        cleaned = text.strip()

        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`").strip()
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:].strip()

        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end < start:
            raise ValueError("model did not return a JSON object")

        return json.loads(cleaned[start : end + 1])

    def decide(self, context: str) -> ChatAction:
        response = self.client.responses.create(
            model=self.model,
            instructions=SYSTEM_PROMPT,
            input=context,
        )

        raw = response.output_text or ""
        data = self._extract_json(raw)

        action = str(data.get("action") or "ignore").lower()
        reason = str(data.get("reason") or "model")

        if action == "reply":
            text = str(data.get("text") or "").strip()
            if not text:
                return ChatAction(kind="ignore", reason="empty_model_reply")
            return ChatAction(kind="reply", reason=reason, text=text)

        if action == "react":
            reaction = ChatReaction.from_name(
                str(data.get("reaction") or "")
            )
            if reaction is None:
                return ChatAction(kind="ignore", reason="invalid_model_reaction")
            return ChatAction(
                kind="react",
                reason=reason,
                reaction=reaction,
            )

        return ChatAction(kind="ignore", reason=reason)
