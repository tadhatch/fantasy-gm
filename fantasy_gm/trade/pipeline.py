# fantasy_gm/trade/pipeline.py

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict

from fantasy_gm.context.evaluation_worker import board_player_from_roster_entry
from fantasy_gm.context.postgres_store import PostgresContextStore
from fantasy_gm.context.router import ContextModelRouter
from fantasy_gm.espn.client import ESPNClient
from fantasy_gm.espn.constants import POSITION_IDS
from fantasy_gm.espn.roster import (
    current_scoring_period,
    load_all_rosters,
    load_team_roster,
)
from fantasy_gm.espn.transactions import ESPNTransactionsClient
from fantasy_gm.models.roster import TeamRoster
from fantasy_gm.waiver.llm import call_structured_json

from .models import (
    CandidateTrade,
    DeepDiveRequest,
    GMDecision,
    PartnerCandidate,
    RosterAnalysis,
    TradeChip,
    TradeRunResult,
)
from .store import TradeDecisionStore


logger = logging.getLogger(__name__)

ROSTER_SYSTEM = """
You are the roster analyst for an autonomous fantasy football GM,
specifically thinking about trades rather than waivers. Identify real
positional needs (where the roster is genuinely thin or exposed, not just
"could theoretically use more") and trade chips: players valuable enough
that another team would actually want them, where the roster has surplus
depth or a positional logjam. A trade chip is NOT the same as a droppable
bench player -- it should be someone with real value to offer, chosen
because moving them addresses a logjam or funds a need, not because
they're the worst player on the roster.
Return ONLY valid JSON matching the requested schema. No markdown.
"""

TRADE_REASONING_SYSTEM = """
You are the trade-reasoning stage for an autonomous fantasy football GM.
Given our roster analysis and one or more shortlisted trade partners
(already matched by a Python pre-filter on positional surplus/need, not
by you), propose concrete, mutually plausible trades: specific players
offered and requested, framed as something the OTHER team would
realistically accept, not just what benefits us. Consider roster
construction on both sides, not raw player rankings in isolation. You may
request up to {deep_dive_budget} deep-dive research calls (fresh web
search) on specific players -- from either roster -- if resolving real
uncertainty about them would change your recommendation. Only request one
where it would actually change the decision.
Return ONLY valid JSON matching the requested schema. No markdown.
"""

GM_DECISION_SYSTEM = """
You are the final decision stage for an autonomous fantasy football GM,
deciding on a trade proposal. You are given the prior reasoning and any
fresh deep-dive research. Your only job is to convert that into ONE
strict, actionable decision: propose one specific trade, or decide none
of the candidates are worth proposing. Do not re-litigate the reasoning.
Be decisive and terse.
Return ONLY valid JSON matching the requested schema. No markdown.
"""


def run_trade_pipeline(
    client: ESPNClient,
    *,
    team_id: int,
    partner_candidate_limit: int = 3,
    deep_dive_budget: int = 2,
    attempt_transaction: bool = True,
    confirm: bool = False,
    store: PostgresContextStore | None = None,
    decision_store: TradeDecisionStore | None = None,
    router: ContextModelRouter | None = None,
) -> TradeRunResult:
    """
    Roster analysis -> Python-only partner matching (no LLM call) ->
    trade-proposal reasoning -> GM decision. 3 core reasoning calls plus
    up to `deep_dive_budget` fresh web-search calls the reasoning stage
    can request for specific players.

    Shadows unless confirm=True AND FANTASY_GM_TRANSACTIONS_MODE=live --
    same double-gate pattern as lineup/waiver. This was a deliberate,
    explicit policy change, not a default flip: confirm defaults to
    False, so nothing submits differently until it's set.
    """
    store = store or PostgresContextStore()
    decision_store = decision_store or TradeDecisionStore()

    if router is None:
        router = ContextModelRouter(
            luna_model=os.getenv("FANTASY_GM_CONTEXT_MODEL", "gpt-5.6-luna"),
            terra_model=os.getenv(
                "FANTASY_GM_DEEP_DIVE_MODEL", "gpt-5.6-terra"
            ),
        )

    reasoning_model = os.getenv("FANTASY_GM_CONTEXT_MODEL", "gpt-5.6-luna")
    gm_model = os.getenv("FANTASY_GM_GM_DECISION_MODEL", reasoning_model)

    calls_used = 0
    cached_context = store.load_all()

    all_rosters = load_all_rosters(client)
    our_roster = all_rosters.get(team_id) or load_team_roster(
        client, team_id
    )

    # Stage 1: roster analysis.
    roster_payload = _roster_payload(our_roster, cached_context)
    stage1 = call_structured_json(
        model=reasoning_model,
        system=ROSTER_SYSTEM,
        user=(
            f"Our current roster:\n{json.dumps(roster_payload, indent=2)}\n\n"
            "Return JSON:\n"
            "{\n"
            '  "strengths": ["short phrases"],\n'
            '  "weaknesses": ["short phrases"],\n'
            '  "positional_needs": ["QB"|"RB"|"WR"|"TE"|"K"|"D/ST", ...],\n'
            '  "trade_chips": '
            '[{"espn_id": int, "name": str, "reason": str}],\n'
            '  "summary": "2-3 sentence overview"\n'
            "}"
        ),
    )
    calls_used += 1
    roster_analysis = _parse_roster_analysis(stage1)

    # Stage 2: partner matching -- pure Python, no LLM call. Mirrors the
    # waiver pipeline's "pre-filter before spending a call" approach, just
    # matching teams instead of free agents.
    partner_candidates = _find_partner_candidates(
        our_team_id=team_id,
        all_rosters=all_rosters,
        our_needs=roster_analysis.positional_needs,
        our_trade_chips=roster_analysis.trade_chips,
        limit=partner_candidate_limit,
    )

    if not partner_candidates:
        decision = GMDecision(
            action="no_move",
            reasoning="No team has a positional fit worth proposing a trade over right now.",
        )
        result = TradeRunResult(
            roster_analysis=roster_analysis,
            partner_candidates=[],
            candidate_trades=[],
            deep_dives_used=[],
            decision=decision,
            calls_used=calls_used,
            executed=False,
        )
        decision_store.record(team_id=team_id, result=result)
        _mark_run()
        return result

    # Stage 3: trade-proposal reasoning over the shortlisted partners.
    partners_payload = [
        {
            "team_id": p.team_id,
            "team_name": p.team_name,
            "fit_score": p.fit_score,
            "their_surplus_positions": p.their_surplus_positions,
            "positions_they_need_from_us": p.positions_they_need_from_us,
            "roster": _roster_payload(
                all_rosters[p.team_id], cached_context
            ),
        }
        for p in partner_candidates
    ]
    stage3 = call_structured_json(
        model=reasoning_model,
        system=TRADE_REASONING_SYSTEM.format(
            deep_dive_budget=deep_dive_budget
        ),
        user=(
            f"Our roster analysis:\n{json.dumps(_analysis_payload(roster_analysis), indent=2)}\n\n"
            f"Shortlisted trade partners:\n{json.dumps(partners_payload, indent=2)}\n\n"
            "Return JSON:\n"
            "{\n"
            '  "candidate_trades": [{"partner_team_id": int, '
            '"partner_team_name": str, "offer_espn_ids": [int], '
            '"offer_names": [str], "request_espn_ids": [int], '
            '"request_names": [str], "reasoning": str, "priority": int}],\n'
            '  "deep_dive_requests": '
            f'[{{"espn_id": int, "name": str, "reason": str}}] '
            f"(at most {deep_dive_budget})\n"
            "}"
        ),
    )
    calls_used += 1
    candidate_trades = _parse_candidate_trades(stage3)
    deep_dive_requests = _parse_deep_dive_requests(
        stage3, budget=deep_dive_budget
    )

    deep_dive_results = []
    for request in deep_dive_requests:
        board_player = _minimal_board_player(
            request, our_roster, all_rosters
        )
        routed = router.terra.research(
            espn_id=board_player.espn_id,
            player_name=board_player.name,
            position=board_player.position,
            nfl_team=board_player.nfl_team_id,
            quantitative_context={"deep_dive_reason": request.reason},
        )
        store.put(routed)
        deep_dive_results.append(routed)
        calls_used += 1

    # Stage 4: GM decision.
    stage4 = call_structured_json(
        model=gm_model,
        system=GM_DECISION_SYSTEM,
        user=(
            "Candidate trades from the reasoning stage:\n"
            f"{json.dumps([asdict(t) for t in candidate_trades], indent=2)}\n\n"
            "Fresh deep-dive research (if any):\n"
            f"{json.dumps(_deep_dive_payload(deep_dive_results), indent=2)}\n\n"
            "Return JSON:\n"
            "{\n"
            '  "action": "propose_trade"|"no_move",\n'
            '  "partner_team_id": int|null,\n'
            '  "partner_team_name": str|null,\n'
            '  "offer_player_ids": [int],\n'
            '  "offer_player_names": [str],\n'
            '  "request_player_ids": [int],\n'
            '  "request_player_names": [str],\n'
            '  "message": "short message to the other team, empty if no_move",\n'
            '  "confidence": number 0 to 1,\n'
            '  "reasoning": "one or two sentences"\n'
            "}"
        ),
    )
    calls_used += 1
    decision = _parse_decision(stage4)

    executed = False
    if (
        attempt_transaction
        and decision.action == "propose_trade"
        and decision.partner_team_id
        and decision.offer_player_ids
        and decision.request_player_ids
    ):
        txn = ESPNTransactionsClient(client)
        response = txn.propose_trade(
            proposing_team_id=team_id,
            receiving_team_id=decision.partner_team_id,
            players_offered=decision.offer_player_ids,
            players_requested=decision.request_player_ids,
            scoring_period_id=current_scoring_period(client),
            message=decision.message,
            confirm=confirm,
        )
        executed = response is not None

    result = TradeRunResult(
        roster_analysis=roster_analysis,
        partner_candidates=partner_candidates,
        candidate_trades=candidate_trades,
        deep_dives_used=deep_dive_requests,
        decision=decision,
        calls_used=calls_used,
        executed=executed,
    )

    decision_store.record(team_id=team_id, result=result)
    _mark_run()

    return result


def _mark_run() -> None:
    try:
        from fantasy_gm.railway.task_runs import TaskRunStore

        TaskRunStore().mark_run("trade")
    except Exception:
        # Bookkeeping only — never block a real trade decision on it.
        logger.exception("trade: failed to record task run")


def _roster_payload(roster: TeamRoster, cached_context) -> list[dict]:
    payload = []
    for entry in roster.entries:
        ctx = cached_context.get(entry.player_id)
        payload.append(
            {
                "espn_id": entry.player_id,
                "name": entry.name,
                "position": POSITION_IDS.get(
                    entry.default_position_id,
                    str(entry.default_position_id or "?"),
                ),
                "injury_status": entry.injury_status,
                "recent_evaluation": (
                    {
                        "delta": ctx.direct_delta,
                        "confidence": ctx.confidence,
                        "summary": ctx.summary,
                    }
                    if ctx
                    else None
                ),
            }
        )
    return payload


def _team_position_counts(roster: TeamRoster) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in roster.entries:
        position = POSITION_IDS.get(entry.default_position_id, "?")
        counts[position] = counts.get(position, 0) + 1
    return counts


def _find_partner_candidates(
    *,
    our_team_id: int,
    all_rosters: dict[int, TeamRoster],
    our_needs: list[str],
    our_trade_chips: list[TradeChip],
    limit: int,
) -> list[PartnerCandidate]:
    """
    Deterministic pre-filter, no LLM call: which other teams look like a
    plausible trade fit, purely from positional headcount versus the
    league's own average at each position. Mirrors the waiver pipeline's
    "narrow with Python before spending a reasoning call" approach.
    """
    our_roster = all_rosters.get(our_team_id)
    if our_roster is None:
        return []

    trade_chip_positions = {
        POSITION_IDS.get(entry.default_position_id, "?")
        for entry in our_roster.entries
        if entry.player_id in {c.espn_id for c in our_trade_chips}
    }

    position_totals: dict[str, list[int]] = {}
    for roster in all_rosters.values():
        for position, count in _team_position_counts(roster).items():
            position_totals.setdefault(position, []).append(count)
    league_avg = {
        position: sum(counts) / len(counts)
        for position, counts in position_totals.items()
    }

    candidates: list[PartnerCandidate] = []
    for team_id, roster in all_rosters.items():
        if team_id == our_team_id:
            continue

        their_counts = _team_position_counts(roster)
        their_surplus = [
            position
            for position in our_needs
            if their_counts.get(position, 0) > league_avg.get(position, 0)
        ]
        they_need_from_us = [
            position
            for position in trade_chip_positions
            if their_counts.get(position, 0) < league_avg.get(position, 0)
        ]

        fit_score = float(len(their_surplus) + len(they_need_from_us))
        if fit_score <= 0:
            continue

        candidates.append(
            PartnerCandidate(
                team_id=team_id,
                team_name=roster.team_name,
                fit_score=fit_score,
                their_surplus_positions=their_surplus,
                positions_they_need_from_us=they_need_from_us,
            )
        )

    candidates.sort(key=lambda c: -c.fit_score)
    return candidates[:limit]


def _minimal_board_player(
    request: DeepDiveRequest,
    our_roster: TeamRoster,
    all_rosters: dict[int, TeamRoster],
):
    for entry in our_roster.entries:
        if entry.player_id == request.espn_id:
            return board_player_from_roster_entry(entry)

    for roster in all_rosters.values():
        for entry in roster.entries:
            if entry.player_id == request.espn_id:
                return board_player_from_roster_entry(entry)

    from fantasy_gm.board.models import BoardPlayer

    return BoardPlayer(
        espn_id=request.espn_id,
        name=request.name,
        position="?",
        projected_points=0.0,
        replacement_points=0.0,
        vor=0.0,
        nfl_team_id=None,
        injury_status=None,
    )


def _analysis_payload(analysis: RosterAnalysis) -> dict:
    return {
        "strengths": analysis.strengths,
        "weaknesses": analysis.weaknesses,
        "positional_needs": analysis.positional_needs,
        "trade_chips": [asdict(c) for c in analysis.trade_chips],
        "summary": analysis.summary,
    }


def _deep_dive_payload(results) -> list[dict]:
    return [
        {
            "espn_id": r.espn_id,
            "name": r.player_name,
            "summary": r.summary,
            "direct_delta": r.direct_delta,
            "confidence": r.confidence,
        }
        for r in results
    ]


def _parse_roster_analysis(data: dict) -> RosterAnalysis:
    return RosterAnalysis(
        strengths=list(data.get("strengths", [])),
        weaknesses=list(data.get("weaknesses", [])),
        positional_needs=list(data.get("positional_needs", [])),
        trade_chips=[
            TradeChip(
                espn_id=int(c["espn_id"]),
                name=str(c.get("name", "")),
                reason=str(c.get("reason", "")),
            )
            for c in data.get("trade_chips", [])
            if isinstance(c, dict) and "espn_id" in c
        ],
        summary=str(data.get("summary", "")),
    )


def _parse_candidate_trades(data: dict) -> list[CandidateTrade]:
    return [
        CandidateTrade(
            partner_team_id=int(t["partner_team_id"]),
            partner_team_name=str(t.get("partner_team_name", "")),
            offer_espn_ids=[int(i) for i in t.get("offer_espn_ids", [])],
            offer_names=list(t.get("offer_names", [])),
            request_espn_ids=[
                int(i) for i in t.get("request_espn_ids", [])
            ],
            request_names=list(t.get("request_names", [])),
            reasoning=str(t.get("reasoning", "")),
            priority=int(t.get("priority", 1)),
        )
        for t in data.get("candidate_trades", [])
        if isinstance(t, dict) and "partner_team_id" in t
    ]


def _parse_deep_dive_requests(
    data: dict, *, budget: int
) -> list[DeepDiveRequest]:
    requests = [
        DeepDiveRequest(
            espn_id=int(r["espn_id"]),
            name=str(r.get("name", "")),
            reason=str(r.get("reason", "")),
        )
        for r in data.get("deep_dive_requests", [])
        if isinstance(r, dict) and "espn_id" in r
    ]
    return requests[:budget]


def _parse_decision(data: dict) -> GMDecision:
    return GMDecision(
        action=str(data.get("action", "no_move")),
        partner_team_id=data.get("partner_team_id"),
        partner_team_name=data.get("partner_team_name"),
        offer_player_ids=[
            int(i) for i in data.get("offer_player_ids", []) or []
        ],
        offer_player_names=list(data.get("offer_player_names", []) or []),
        request_player_ids=[
            int(i) for i in data.get("request_player_ids", []) or []
        ],
        request_player_names=list(
            data.get("request_player_names", []) or []
        ),
        message=str(data.get("message", "")),
        confidence=float(data.get("confidence", 0.0)),
        reasoning=str(data.get("reasoning", "")),
    )
