# fantasy_gm/waiver/pipeline.py

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict

from fantasy_gm.context.evaluation_worker import board_player_from_pool_entry
from fantasy_gm.context.postgres_store import PostgresContextStore
from fantasy_gm.context.router import ContextModelRouter
from fantasy_gm.espn.client import ESPNClient
from fantasy_gm.espn.constants import LINEUP_SLOT_IDS, POSITION_IDS
from fantasy_gm.espn.roster import current_scoring_period, load_team_roster
from fantasy_gm.espn.transactions import ESPNTransactionsClient

from .llm import call_structured_json
from .models import (
    CandidateMove,
    DeepDiveRequest,
    ExpendablePlayer,
    FreeAgentShortlistEntry,
    GMDecision,
    RosterAnalysis,
    WaiverRunResult,
)
from .store import WaiverDecisionStore


logger = logging.getLogger(__name__)

ROSTER_SYSTEM = """
You are the roster analyst for an autonomous fantasy football GM.
Reason about roster construction, not raw talent in isolation:
positional depth, injury exposure, bye-week clustering, and which bench
players are genuinely expendable versus just healthy depth resting
behind a starter. Recent-evaluation notes (when present) are real-world
context already gathered — injuries, role changes, news — weight them
appropriately alongside roster composition.
Return ONLY valid JSON matching the requested schema. No markdown.
"""

SCREENING_SYSTEM = """
You are screening fantasy football free agents for waiver consideration.
You are given a pre-filtered, ranked pool — not the whole league — so
assume real signal already went into producing it. Your job is to pick
the players genuinely worth the deep-evaluation stage, weighted toward
the team's stated positional needs, not just raw ownership rank.
Return ONLY valid JSON matching the requested schema. No markdown.
"""

DECISION_REASONING_SYSTEM = """
You are the decision-reasoning stage for an autonomous fantasy football
GM. Given a roster analysis and a shortlist of free agents, reason
about actual ADD -> DROP combinations: opportunity cost of the drop,
upside of the add, remaining schedule, roster construction (not just
"is the add better than the drop in a vacuum"), and waiver priority
cost if relevant. You may request up to {deep_dive_budget} deep-dive
research calls (fresh web search) on specific players — from either the
shortlist or the roster — if resolving real uncertainty about them
would change your recommendation (major injury news, a big recent game,
a hot/cold streak, a role change, a contract/trade story). Only request
one where it would actually change the decision; do not request one for
every player.
Return ONLY valid JSON matching the requested schema. No markdown.
"""

GM_DECISION_SYSTEM = """
You are the final decision stage for an autonomous fantasy football GM.
You are given the prior reasoning and any fresh deep-dive research.
Your only job is to convert that into ONE strict, actionable decision.
Do not re-litigate the reasoning — pick the single best candidate move,
or decide no move is worth making this run. Be decisive and terse.
Return ONLY valid JSON matching the requested schema. No markdown.
"""


def run_waiver_pipeline(
    client: ESPNClient,
    *,
    team_id: int,
    free_agent_pool_size: int = 60,
    shortlist_size: int = 15,
    deep_dive_budget: int = 3,
    attempt_transaction: bool = True,
    confirm: bool = False,
    store: PostgresContextStore | None = None,
    decision_store: WaiverDecisionStore | None = None,
    router: ContextModelRouter | None = None,
) -> WaiverRunResult:
    """
    The full roster -> free-agent -> decision -> GM-decision pipeline.

    Four core reasoning calls (roster evaluation, FA screening, deep
    evaluation/decision, GM decision) plus up to `deep_dive_budget` fresh
    web-search calls the decision stage can request for specific players
    it's genuinely uncertain about. Total OpenAI calls per run: 4 to
    4 + deep_dive_budget.
    """
    store = store or PostgresContextStore()
    decision_store = decision_store or WaiverDecisionStore()

    if router is None:
        router = ContextModelRouter(
            luna_model=os.getenv("FANTASY_GM_CONTEXT_MODEL", "gpt-5.6-luna"),
            terra_model=os.getenv(
                "FANTASY_GM_DEEP_DIVE_MODEL", "gpt-5.6-terra"
            ),
        )

    reasoning_model = os.getenv("FANTASY_GM_CONTEXT_MODEL", "gpt-5.6-luna")
    gm_model = os.getenv("FANTASY_GM_GM_DECISION_MODEL", reasoning_model)

    try:
        result = _run_waiver_pipeline_body(
            client,
            team_id=team_id,
            free_agent_pool_size=free_agent_pool_size,
            shortlist_size=shortlist_size,
            deep_dive_budget=deep_dive_budget,
            attempt_transaction=attempt_transaction,
            confirm=confirm,
            store=store,
            router=router,
            reasoning_model=reasoning_model,
            gm_model=gm_model,
        )
    except Exception:
        # _waiver_due() (a daily checkpoint) only advances once
        # mark_run("waiver") is called -- without marking a failed
        # attempt too, a persistent failure (e.g. an OpenAI outage) means
        # this stays "due" on every single poll forever instead of
        # waiting for tomorrow's checkpoint, same class of bug already
        # fixed for incoming trades and the evaluation worker.
        _mark_waiver_attempt()
        raise

    decision_store.record(team_id=team_id, result=result)
    _mark_waiver_attempt()

    return result


def _mark_waiver_attempt() -> None:
    try:
        from fantasy_gm.railway.task_runs import TaskRunStore

        TaskRunStore().mark_run("waiver")
    except Exception:
        # Bookkeeping only — never block a real waiver decision on it.
        logger.exception("waiver: failed to record task run")


def _run_waiver_pipeline_body(
    client: ESPNClient,
    *,
    team_id: int,
    free_agent_pool_size: int,
    shortlist_size: int,
    deep_dive_budget: int,
    attempt_transaction: bool,
    confirm: bool,
    store: PostgresContextStore,
    router: ContextModelRouter,
    reasoning_model: str,
    gm_model: str,
) -> WaiverRunResult:
    calls_used = 0
    cached_context = store.load_all()

    roster = load_team_roster(client, team_id)
    rostered_ids = {e.player_id for e in roster.entries}

    # Stage 1: roster evaluation.
    roster_payload = _roster_payload(roster, cached_context)
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
            '  "expendable_players": '
            '[{"espn_id": int, "name": str, "reason": str}],\n'
            '  "summary": "2-3 sentence overview"\n'
            "}"
        ),
    )
    calls_used += 1
    roster_analysis = _parse_roster_analysis(stage1)

    # Stage 2: free-agent screening (Python pre-filter, then one call).
    pool = _prefilter_free_agents(
        client,
        rostered_ids=rostered_ids,
        positional_needs=roster_analysis.positional_needs,
        pool_size=free_agent_pool_size,
    )
    pool_payload = [
        {
            "espn_id": p.espn_id,
            "name": p.name,
            "position": p.position,
            "percent_owned": p.percent_owned,
            "injury_status": p.injury_status,
            "recent_evaluation": (
                {
                    "delta": ctx.direct_delta,
                    "confidence": ctx.confidence,
                    "summary": ctx.summary,
                }
                if (ctx := cached_context.get(p.espn_id))
                else None
            ),
        }
        for p in pool
    ]
    stage2 = call_structured_json(
        model=reasoning_model,
        system=SCREENING_SYSTEM,
        user=(
            f"Positional needs: {roster_analysis.positional_needs}\n\n"
            f"Candidate pool:\n{json.dumps(pool_payload, indent=2)}\n\n"
            f"Pick the {shortlist_size} most worth deeper evaluation.\n"
            "Return JSON:\n"
            "{\n"
            '  "shortlist": [{"espn_id": int, "name": str, '
            '"position": str, "reason": str}]\n'
            "}"
        ),
    )
    calls_used += 1
    shortlist = _parse_shortlist(stage2)

    # Stage 3: deep evaluation / decision reasoning.
    stage3 = call_structured_json(
        model=reasoning_model,
        system=DECISION_REASONING_SYSTEM.format(
            deep_dive_budget=deep_dive_budget
        ),
        user=(
            f"Roster analysis:\n{json.dumps(_analysis_payload(roster_analysis), indent=2)}\n\n"
            f"Shortlisted free agents:\n{json.dumps([asdict(s) for s in shortlist], indent=2)}\n\n"
            "Return JSON:\n"
            "{\n"
            '  "candidate_moves": [{"add_espn_id": int, "add_name": str, '
            '"drop_espn_id": int|null, "drop_name": str|null, '
            '"reasoning": str, "priority": int}],\n'
            '  "deep_dive_requests": '
            f'[{{"espn_id": int, "name": str, "reason": str}}] '
            f"(at most {deep_dive_budget})\n"
            "}"
        ),
    )
    calls_used += 1
    candidate_moves = _parse_candidate_moves(stage3)
    deep_dive_requests = _parse_deep_dive_requests(
        stage3, budget=deep_dive_budget
    )

    # Reserve budget: fresh web-search research on specifically
    # requested players only — not a blanket per-player sweep.
    deep_dive_results = []
    for request in deep_dive_requests:
        board_player = _minimal_board_player(request, roster, pool)
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
            "Candidate moves from the reasoning stage:\n"
            f"{json.dumps([asdict(m) for m in candidate_moves], indent=2)}\n\n"
            "Fresh deep-dive research (if any):\n"
            f"{json.dumps(_deep_dive_payload(deep_dive_results), indent=2)}\n\n"
            "Return JSON:\n"
            "{\n"
            '  "action": "add_drop"|"no_move",\n'
            '  "add_player_id": int|null,\n'
            '  "add_player_name": str|null,\n'
            '  "drop_player_id": int|null,\n'
            '  "drop_player_name": str|null,\n'
            '  "use_waiver": bool,\n'
            '  "faab_bid": int|null,\n'
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
        and decision.action == "add_drop"
        and decision.add_player_id
    ):
        txn = ESPNTransactionsClient(client)
        # The GM-decision stage isn't told whether this league runs FAAB
        # or traditional priority waivers, so it can propose a bid amount
        # that doesn't apply here — this league uses priority waivers
        # (isUsingAcquisitionBudget is False), so a bid is meaningless and
        # is dropped before it ever reaches ESPN, regardless of what the
        # LLM guessed. The recorded decision (decision_store.record(), in
        # run_waiver_pipeline() once this returns) still keeps whatever
        # the LLM originally proposed, for audit purposes -- only the
        # actual submission is corrected.
        # `confirm` is the same double gate as every other transaction:
        # the caller must pass it AND FANTASY_GM_TRANSACTIONS_MODE must be
        # live, or this always just shadow-logs.
        bid_amount = (
            decision.faab_bid if _league_uses_faab(client) else None
        )
        response = txn.add_drop(
            team_id=team_id,
            add_player_id=decision.add_player_id,
            drop_player_id=decision.drop_player_id,
            scoring_period_id=current_scoring_period(client),
            via_waiver=decision.use_waiver,
            bid_amount=bid_amount,
            confirm=confirm,
        )
        executed = response is not None

    return WaiverRunResult(
        roster_analysis=roster_analysis,
        shortlist=shortlist,
        candidate_moves=candidate_moves,
        deep_dives_used=deep_dive_requests,
        decision=decision,
        calls_used=calls_used,
        executed=executed,
    )


def _league_uses_faab(client: ESPNClient) -> bool:
    try:
        data = client.get_league(["mSettings"])
        return bool(
            data["settings"]["acquisitionSettings"][
                "isUsingAcquisitionBudget"
            ]
        )
    except Exception:
        logger.exception(
            "waiver: failed to read acquisition settings; "
            "assuming no FAAB bid"
        )
        return False


def _roster_payload(roster, cached_context) -> list[dict]:
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
                "lineup_slot": LINEUP_SLOT_IDS.get(
                    entry.lineup_slot_id, entry.lineup_slot_id
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


def _prefilter_free_agents(
    client: ESPNClient,
    *,
    rostered_ids: set[int],
    positional_needs: list[str],
    pool_size: int,
) -> list:
    pool_data = client.get_player_pool(limit=max(1000, pool_size * 10))
    raw_pool = (
        pool_data.get("players", [])
        if isinstance(pool_data, dict)
        else pool_data
    )

    candidates = []
    for entry in raw_pool:
        # board_player_from_pool_entry() already filters to free agents.
        candidate = board_player_from_pool_entry(entry)
        if candidate is None or candidate.espn_id in rostered_ids:
            continue

        candidates.append(candidate)

    def sort_key(c):
        need_first = 0 if c.position in positional_needs else 1
        return (need_first, -(c.percent_owned or 0.0))

    candidates.sort(key=sort_key)
    return candidates[:pool_size]


def _minimal_board_player(request: DeepDiveRequest, roster, pool):
    for entry in roster.entries:
        if entry.player_id == request.espn_id:
            from fantasy_gm.context.evaluation_worker import (
                board_player_from_roster_entry,
            )

            return board_player_from_roster_entry(entry)

    for candidate in pool:
        if candidate.espn_id == request.espn_id:
            return candidate

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
        "expendable_players": [
            asdict(e) for e in analysis.expendable_players
        ],
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
        expendable_players=[
            ExpendablePlayer(
                espn_id=int(e["espn_id"]),
                name=str(e.get("name", "")),
                reason=str(e.get("reason", "")),
            )
            for e in data.get("expendable_players", [])
            if isinstance(e, dict) and "espn_id" in e
        ],
        summary=str(data.get("summary", "")),
    )


def _parse_shortlist(data: dict) -> list[FreeAgentShortlistEntry]:
    return [
        FreeAgentShortlistEntry(
            espn_id=int(e["espn_id"]),
            name=str(e.get("name", "")),
            position=str(e.get("position", "")),
            reason=str(e.get("reason", "")),
        )
        for e in data.get("shortlist", [])
        if isinstance(e, dict) and "espn_id" in e
    ]


def _parse_candidate_moves(data: dict) -> list[CandidateMove]:
    return [
        CandidateMove(
            add_espn_id=int(m["add_espn_id"]),
            add_name=str(m.get("add_name", "")),
            drop_espn_id=(
                int(m["drop_espn_id"])
                if m.get("drop_espn_id") is not None
                else None
            ),
            drop_name=m.get("drop_name"),
            reasoning=str(m.get("reasoning", "")),
            priority=int(m.get("priority", 1)),
        )
        for m in data.get("candidate_moves", [])
        if isinstance(m, dict) and "add_espn_id" in m
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
        add_player_id=data.get("add_player_id"),
        add_player_name=data.get("add_player_name"),
        drop_player_id=data.get("drop_player_id"),
        drop_player_name=data.get("drop_player_name"),
        use_waiver=bool(data.get("use_waiver", False)),
        faab_bid=data.get("faab_bid"),
        confidence=float(data.get("confidence", 0.0)),
        reasoning=str(data.get("reasoning", "")),
    )
