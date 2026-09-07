# fantasy_gm/context/dst_news_scan.py

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from openai import OpenAI

from fantasy_gm.board.models import BoardPlayer

from .models import AIContextResult, EvidenceItem
from .news_scan import _clamp, _finding_to_result, _parse_json


DST_BROAD_SCAN_SYSTEM = """
You are a team-defense (D/ST) scanner for an autonomous fantasy football
GM. A fantasy D/ST entry represents an entire team's defense and special
teams unit, not one person -- research it differently from an individual
skill-position player. For each team defense listed below, look for:

- Injuries to KEY starting defensive personnel (starting pass rushers,
  top cornerbacks/safeties, middle linebackers) that would meaningfully
  change the unit's expected performance this week -- not depth-chart
  trivia further down the roster.
- Roster or personnel movement affecting the defense: trades, cuts,
  signings, suspensions of defensive starters, or a coordinator/scheme
  change.
- Genuinely unusual off-field or environmental circumstances specific to
  this team, its facility, or its travel this week that are outside the
  team's control -- venue/stadium problems, unusual travel disruption,
  facility issues, or any other concrete real-world event a standard
  projection wouldn't already reflect. This category exists specifically
  to catch the "freak occurrence" case (e.g. a stadium power failure, a
  natural disaster near the facility, a bizarre logistics problem) --
  do NOT speculate or invent one; only report something real and sourced.

Do NOT evaluate normal on-field performance trends, opponent matchup
quality/strength of schedule, or routine game-plan discussion -- a
separate deterministic system already covers on-field projections.

You are given a list of team defenses currently relevant to this fantasy
team. Only report findings for defenses on this list. If nothing notable
is happening for a defense, do not include it in the response at all --
a short or empty findings list is a completely normal, expected result.

For each finding, decide whether it is significant or uncertain enough
that a focused follow-up investigation on that one defense would
meaningfully improve confidence. Most findings should NOT need this.

Return ONLY valid JSON matching the requested schema. No markdown.
"""

DST_DEEPDIVE_SYSTEM = """
You are the current-context analyst for a team defense/special-teams
(D/ST) fantasy entry, for an autonomous fantasy football GM.

A D/ST entry represents an entire team's defensive and special-teams
unit. Research CURRENT information a standard fantasy projection
wouldn't already reflect:

- injuries to key starting defensive personnel and their expected effect
  on the unit
- personnel moves: trades, cuts, signings, suspensions of defensive
  starters
- coordinator or scheme changes
- genuinely unusual off-field/environmental circumstances specific to
  this team, facility, or travel (the "freak occurrence" case) -- only
  report something real and sourced, never invented

Do not evaluate normal opponent matchup quality or on-field trends --
that is handled elsewhere.

The direct_delta is a CONTEXT adjustment to an existing fantasy score:
  -15 = extraordinary negative context
    0 = no material current adjustment
  +15 = extraordinary positive context
Most items should be between -5 and +5.

Return ONLY valid JSON matching the requested object. No markdown.
"""


def run_dst_news_scan(
    teams: list[BoardPlayer],
    *,
    broad_model: str,
    deep_dive_model: str,
    max_calls: int = 2,
) -> list[AIContextResult]:
    """
    D/ST counterpart to news_scan.run_news_scan: same broad-scan-then-
    escalate shape, but with prompts written for a team-defense entity
    instead of an individual player -- injuries/personnel/off-field
    questions that make sense for a person don't make sense for a team
    (contract holdouts), and vice versa (a team can have a stadium
    problem; a person can't). Kept as its own small budget, separate
    from the individual-player scan's max_calls, since D/ST entries are
    few and this shouldn't eat into that budget's existing tuning.
    """
    if not teams or max_calls <= 0:
        return []

    findings = _broad_dst_scan(teams, model=broad_model)
    calls_used = 1

    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    results: list[AIContextResult] = []

    for finding in findings:
        if finding.get("needs_deeper_dive") and calls_used < max_calls:
            team = _find_team(teams, finding["espn_id"])
            if team is not None:
                results.append(
                    _dst_deep_dive(
                        client,
                        model=deep_dive_model,
                        team=team,
                        broad_finding=finding,
                    )
                )
                calls_used += 1
                continue

        results.append(_finding_to_result(finding, model=broad_model))

    return results


def _broad_dst_scan(teams: list[BoardPlayer], *, model: str) -> list[dict]:
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    team_list = [{"espn_id": t.espn_id, "name": t.name} for t in teams]

    user = f"""
Team defenses currently relevant to this fantasy team:
{json.dumps(team_list, indent=2)}

Return JSON:
{{
  "findings": [
    {{
      "espn_id": int,
      "player_name": "string (the D/ST's name, e.g. 'Seahawks D/ST')",
      "summary": "short factual synthesis",
      "direct_delta": number from -15 to 15,
      "confidence": number from 0 to 1,
      "category": "injury|personnel|coaching|team_environment|news",
      "availability_risk": number 0 to 1,
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
            {"role": "system", "content": DST_BROAD_SCAN_SYSTEM},
            {"role": "user", "content": user},
        ],
    )

    data = _parse_json(response.output_text.strip())

    return [
        f
        for f in data.get("findings", [])
        if isinstance(f, dict) and "espn_id" in f
    ]


def _dst_deep_dive(
    client: OpenAI, *, model: str, team: BoardPlayer, broad_finding: dict
) -> AIContextResult:
    prompt = f"""
Research {team.name} (NFL team identifier {team.nfl_team_id}) as a
fantasy D/ST entry, as of right now.

Existing broad-scan finding to verify/expand on:
{json.dumps({
    "summary": broad_finding.get("summary"),
    "direct_delta": broad_finding.get("direct_delta"),
    "confidence": broad_finding.get("confidence"),
    "reason_for_deeper_dive": broad_finding.get("deeper_dive_reason"),
}, indent=2)}

Return this JSON object:
{{
  "summary": "short factual fantasy-relevant synthesis",
  "direct_delta": number from -15 to 15,
  "confidence": number from 0 to 1,
  "category": "injury|personnel|coaching|team_environment|news",
  "availability_risk": number 0 to 1,
  "evidence": [
    {{
      "title": "article/report title",
      "source": "publisher/source",
      "url": "URL if available",
      "published_at": "date/time if known",
      "claim": "specific fact this source supports",
      "reliability": number 0 to 1
    }}
  ]
}}
"""

    response = client.responses.create(
        model=model,
        tools=[{"type": "web_search"}],
        input=[
            {"role": "system", "content": DST_DEEPDIVE_SYSTEM},
            {"role": "user", "content": prompt},
        ],
    )

    data = _parse_json(response.output_text.strip())

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

    return AIContextResult(
        espn_id=team.espn_id,
        player_name=team.name,
        researched_at=datetime.now(timezone.utc).isoformat(),
        model=model,
        summary=str(data.get("summary") or ""),
        direct_delta=_clamp(
            float(data.get("direct_delta", 0.0)), -15.0, 15.0
        ),
        confidence=_clamp(float(data.get("confidence", 0.0)), 0.0, 1.0),
        category=str(data.get("category") or "news"),
        availability_risk=_clamp(
            float(data.get("availability_risk", 0.0)), 0.0, 1.0
        ),
        evidence=evidence,
    )


def _find_team(teams: list[BoardPlayer], espn_id: int) -> BoardPlayer | None:
    for t in teams:
        if t.espn_id == espn_id:
            return t
    return None
