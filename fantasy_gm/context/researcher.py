from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from openai import OpenAI

from .models import AIContextResult, EvidenceItem, RelatedPlayerEffect


SYSTEM = """
You are the current-context analyst for an autonomous fantasy-football general manager.

Your job is NOT to rank the player from vibes. Quantitative projections, VOR, usage,
efficiency, ADP, and roster construction are calculated elsewhere.

Research CURRENT information that could make those quantitative numbers stale or
incomplete. Use web search aggressively. Prefer official team/NFL sources, Reuters/AP,
major beat reporters, and well-established football publications. Treat unsourced social
media and rumor aggregators as low confidence.

Look for:
- injuries, recovery, surgery, missed practice, conditioning, workload limits
- holdouts, contract disputes, trade requests, team disputes
- suspension or discipline risk
- arrests, lawsuits, allegations, investigations, or other off-field matters ONLY for
  plausible fantasy effects such as distraction, missed practices, team/league discipline,
  travel/court conflicts, or availability. Never infer guilt or truth of an allegation.
- depth-chart changes and first-team reps
- teammate injuries that create/remove opportunity
- coaching/scheme changes
- offensive-line or quarterback changes that materially affect this player
- preseason/camp performance when it contains role/usage evidence
- reports explaining HOW production was achieved: role, routes, carries, targets,
  goal-line work, explosive-play dependence, broken plays, garbage time, etc.

The direct_delta is a CONTEXT adjustment to an existing fantasy score:
  -15 = extraordinary negative context
    0 = no material current adjustment
  +15 = extraordinary positive context
Most items should be between -5 and +5.

Do not double-count known performance. A good 2025 season is already in the quantitative
model. Adjust only when current evidence changes the expected 2026 state or explains why
the baseline may be misleading.

For related_players, identify teammates whose fantasy value plausibly changes because of
the event. Negative news for RB1 can improve RB2/RB3; QB injury can hurt pass catchers;
WR injury can redistribute targets. Do not invent related players.

Return ONLY valid JSON matching the requested object. No markdown.
"""


class OpenAIContextResearcher:
    def __init__(self, model: str | None = None):
        self.model = model or os.getenv("FANTASY_GM_CONTEXT_MODEL", "gpt-5.6-terra")
        self.client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    def research(
        self,
        *,
        espn_id: int,
        player_name: str,
        position: str,
        nfl_team: str | int | None,
        quantitative_context: dict,
    ) -> AIContextResult:
        prompt = f"""
Research {player_name}, position {position}, NFL team identifier/name {nfl_team},
as of right now for a 2026 fantasy football draft.

Existing quantitative context:
{json.dumps(quantitative_context, indent=2)}

Focus especially on developments from the last 30 days, but use older evidence when
needed to explain an ongoing injury/recovery/contract/legal/role situation.

Return this JSON object:
{{
  "summary": "short factual fantasy-relevant synthesis",
  "direct_delta": number from -15 to 15,
  "confidence": number from 0 to 1,
  "category": "injury|role|opportunity|off_field|contract|coaching|team_environment|offensive_line|suspension|news",
  "availability_risk": number 0 to 1,
  "distraction_risk": number 0 to 1,
  "role_change": number from -1 to 1,
  "injury_change": number from -1 to 1,
  "evidence": [
    {{
      "title": "article/report title",
      "source": "publisher/source",
      "url": "URL if available",
      "published_at": "date/time if known",
      "claim": "specific fact this source supports",
      "reliability": number 0 to 1
    }}
  ],
  "related_players": [
    {{
      "player_name": "exact teammate name",
      "relationship": "direct_backup|same_backfield|target_competitor|quarterback|same_offense|other",
      "delta": number from -10 to 10,
      "confidence": number 0 to 1,
      "reason": "why this player's fantasy value changes"
    }}
  ]
}}
"""

        response = self.client.responses.create(
            model=self.model,
            tools=[{"type": "web_search"}],
            input=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt},
            ],
        )

        text = response.output_text.strip()
        data = _parse_json(text)

        evidence = [
            EvidenceItem(
                title=str(e.get("title") or "Untitled source"),
                source=str(e.get("source") or "Unknown"),
                url=e.get("url"),
                published_at=e.get("published_at"),
                claim=e.get("claim"),
                reliability=_clamp(float(e.get("reliability", 0.7)), 0.0, 1.0),
            )
            for e in data.get("evidence", [])[:10]
            if isinstance(e, dict)
        ]

        related = [
            RelatedPlayerEffect(
                player_name=str(r["player_name"]),
                relationship=str(r.get("relationship") or "other"),
                delta=_clamp(float(r.get("delta", 0.0)), -10.0, 10.0),
                confidence=_clamp(float(r.get("confidence", 0.0)), 0.0, 1.0),
                reason=str(r.get("reason") or ""),
            )
            for r in data.get("related_players", [])[:10]
            if isinstance(r, dict) and r.get("player_name")
        ]

        return AIContextResult(
            espn_id=espn_id,
            player_name=player_name,
            researched_at=datetime.now(timezone.utc).isoformat(),
            model=self.model,
            summary=str(data.get("summary") or ""),
            direct_delta=_clamp(float(data.get("direct_delta", 0.0)), -15.0, 15.0),
            confidence=_clamp(float(data.get("confidence", 0.0)), 0.0, 1.0),
            category=str(data.get("category") or "news"),
            availability_risk=_clamp(float(data.get("availability_risk", 0.0)), 0.0, 1.0),
            distraction_risk=_clamp(float(data.get("distraction_risk", 0.0)), 0.0, 1.0),
            role_change=_clamp(float(data.get("role_change", 0.0)), -1.0, 1.0),
            injury_change=_clamp(float(data.get("injury_change", 0.0)), -1.0, 1.0),
            evidence=evidence,
            related_players=related,
            raw_response_id=getattr(response, "id", None),
        )


def _parse_json(text: str) -> dict:
    # tolerate accidental fenced JSON
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


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
