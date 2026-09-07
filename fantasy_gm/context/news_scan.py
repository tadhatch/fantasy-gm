# fantasy_gm/context/news_scan.py

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from openai import OpenAI

from fantasy_gm.board.models import BoardPlayer

from .models import AIContextResult, EvidenceItem
from .researcher import OpenAIContextResearcher


BROAD_SCAN_SYSTEM = """
You are an off-field news scanner for an autonomous fantasy football GM.

Scan major sports news outlets and social media broadly for OFF-FIELD
developments affecting NFL players' fantasy availability or standing:
contract negotiations, holdouts, trade requests/rumors, suspensions or
discipline, legal issues/arrests, personal life events (recovery
timelines, family events, offseason procedures), and off-field
distractions (social media controversy, relationship drama, locker
room/front-office conflict).

Do NOT evaluate on-field performance, statistics, snap counts, target
share, or game usage trends — a separate deterministic system already
covers that. Only report genuinely off-field, real-world developments.

You are given a list of players currently relevant to this fantasy
team (rostered or notable free agents). Only report findings for
players on this list. If nothing notable is happening for a player, do
not include them in the response at all — a short or even empty
findings list is a completely normal, expected result; do not invent a
story to fill space.

For each finding, decide whether it is significant or uncertain enough
that a focused follow-up investigation — spending real, additional
research specifically on that one player — would meaningfully improve
the confidence or size of the adjustment. Most findings should NOT need
this; reserve it for genuinely major or unclear situations (holdout
status unclear, a contract dispute escalating, a legal situation with
real fantasy impact, conflicting reports).

Return ONLY valid JSON matching the requested schema. No markdown.
"""


def run_news_scan(
    players: list[BoardPlayer],
    *,
    broad_model: str,
    deep_dive_model: str,
    max_calls: int = 5,
) -> list[AIContextResult]:
    """
    Off-field "feeling" scan across a whole player pool in a small,
    bounded number of calls, instead of one research call per player.

    One broad web-search call surfaces whichever players actually have
    something going on — most players most days have nothing, so
    researching each individually wastes nearly all of that budget on
    non-findings. Findings the broad scan itself flags as significant or
    unclear get a genuine follow-up deep dive (reusing the existing
    single-player research pipeline), up to whatever's left of
    `max_calls`.
    """
    if not players or max_calls <= 0:
        return []

    findings = _broad_scan(players, model=broad_model)
    calls_used = 1

    deep_diver = OpenAIContextResearcher(model=deep_dive_model)
    results: list[AIContextResult] = []

    for finding in findings:
        if (
            finding.get("needs_deeper_dive")
            and calls_used < max_calls
        ):
            player = _find_player(players, finding["espn_id"])
            if player is not None:
                results.append(
                    deep_diver.research(
                        espn_id=player.espn_id,
                        player_name=player.name,
                        position=player.position,
                        nfl_team=player.nfl_team_id,
                        quantitative_context={
                            "broad_scan_finding": {
                                "summary": finding.get("summary"),
                                "direct_delta": finding.get("direct_delta"),
                                "confidence": finding.get("confidence"),
                                "reason_for_deeper_dive": finding.get(
                                    "deeper_dive_reason"
                                ),
                            }
                        },
                    )
                )
                calls_used += 1
                continue

        results.append(_finding_to_result(finding, model=broad_model))

    return results


def _broad_scan(
    players: list[BoardPlayer],
    *,
    model: str,
) -> list[dict]:
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    player_list = [
        {"espn_id": p.espn_id, "name": p.name, "position": p.position}
        for p in players
    ]

    user = f"""
Players currently relevant to this team:
{json.dumps(player_list, indent=2)}

Return JSON:
{{
  "findings": [
    {{
      "espn_id": int,
      "player_name": "string",
      "summary": "short factual off-field synthesis",
      "direct_delta": number from -15 to 15,
      "confidence": number from 0 to 1,
      "category": "off_field|contract|suspension|news",
      "availability_risk": number 0 to 1,
      "distraction_risk": number 0 to 1,
      "evidence": [
        {{
          "title": "string", "source": "string", "url": "string or null",
          "published_at": "string or null", "claim": "string",
          "reliability": number 0 to 1
        }}
      ],
      "needs_deeper_dive": boolean,
      "deeper_dive_reason": "string, empty if false"
    }}
  ]
}}
"""

    response = client.responses.create(
        model=model,
        tools=[{"type": "web_search"}],
        input=[
            {"role": "system", "content": BROAD_SCAN_SYSTEM},
            {"role": "user", "content": user},
        ],
    )

    data = _parse_json(response.output_text.strip())

    return [
        f
        for f in data.get("findings", [])
        if isinstance(f, dict) and "espn_id" in f
    ]


def _finding_to_result(finding: dict, *, model: str) -> AIContextResult:
    evidence = [
        EvidenceItem(
            title=str(e.get("title") or "Untitled source"),
            source=str(e.get("source") or "Unknown"),
            url=e.get("url"),
            published_at=e.get("published_at"),
            claim=e.get("claim"),
            reliability=_clamp(float(e.get("reliability", 0.7)), 0.0, 1.0),
        )
        for e in finding.get("evidence", [])[:10]
        if isinstance(e, dict)
    ]

    return AIContextResult(
        espn_id=int(finding["espn_id"]),
        player_name=str(finding.get("player_name", "")),
        researched_at=datetime.now(timezone.utc).isoformat(),
        model=model,
        summary=str(finding.get("summary") or ""),
        direct_delta=_clamp(
            float(finding.get("direct_delta", 0.0)), -15.0, 15.0
        ),
        confidence=_clamp(float(finding.get("confidence", 0.0)), 0.0, 1.0),
        category=str(finding.get("category") or "off_field"),
        availability_risk=_clamp(
            float(finding.get("availability_risk", 0.0)), 0.0, 1.0
        ),
        distraction_risk=_clamp(
            float(finding.get("distraction_risk", 0.0)), 0.0, 1.0
        ),
        evidence=evidence,
    )


def _find_player(
    players: list[BoardPlayer], espn_id: int
) -> BoardPlayer | None:
    for p in players:
        if p.espn_id == espn_id:
            return p
    return None


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


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
