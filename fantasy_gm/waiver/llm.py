# fantasy_gm/waiver/llm.py

from __future__ import annotations

import json
import os

from openai import OpenAI


def call_structured_json(*, model: str, system: str, user: str) -> dict:
    """
    Reasoning-only structured call (no web search) — used for stages
    that reason over data already gathered/provided rather than needing
    fresh research. Fresh-research calls go through
    context.router.ContextModelRouter instead, which already handles
    the web-search tool and the AIContextResult schema.
    """
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    response = client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )

    return _parse_json(response.output_text.strip())


def _parse_json(text: str) -> dict:
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].lstrip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise
