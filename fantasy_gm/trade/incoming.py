# fantasy_gm/trade/incoming.py
#
# Detects and evaluates trade proposals another team has sent us.
#
# The read side (mTransactions2) is confirmed live against this league's
# real transaction history: status/isPending/teamId/items(fromTeamId,
# toTeamId) fields all round-trip as expected for real ROSTER/WAIVER
# transactions. No trade has ever happened in this league yet, so the
# exact shape of a *pending* TRADE_PROPOSAL entry is inferred from that
# same envelope, not confirmed against a real one -- treat it the same
# way as every other reverse-engineered payload here until a real
# incoming trade actually shows up.

from __future__ import annotations

import json
import logging
import os

from fantasy_gm.context.postgres_store import PostgresContextStore
from fantasy_gm.context.router import ContextModelRouter
from fantasy_gm.espn.client import ESPNClient
from fantasy_gm.espn.constants import POSITION_IDS
from fantasy_gm.espn.roster import current_scoring_period, load_team_roster
from fantasy_gm.espn.transactions import ESPNTransactionsClient
from fantasy_gm.waiver.llm import call_structured_json

from .models import DeepDiveRequest, IncomingTradeResult, PendingIncomingTrade
from .pipeline import _roster_payload
from .store import IncomingTradeDecisionStore


logger = logging.getLogger(__name__)

INCOMING_TRADE_SYSTEM = """
You are the trade-response evaluator for an autonomous fantasy football
GM. You are given ONE specific trade proposal another team has already
submitted -- your only job is to decide whether to accept or reject it,
not to negotiate, counter, or generate alternatives. Weigh the real value
of what's coming in against what's going out, and how each side changes
roster construction: does giving up these players create a real hole,
even if what's coming in is individually "better"? Does what's coming in
actually address a real need, or just add a name? Use any real-world
context already gathered for these specific players. You may request up
to {deep_dive_budget} deep-dive research calls (fresh web search) on
specific players if resolving real uncertainty about them would change
your decision. Only request one where it would actually change things.
Return ONLY valid JSON matching the requested schema. No markdown.
"""


def find_pending_incoming_trades(
    client: ESPNClient, *, team_id: int
) -> list[PendingIncomingTrade]:
    """
    Every currently-pending trade proposal where another team is the
    proposer and our team is on one side of it — i.e. something we could
    actually accept or reject, not a proposal we ourselves sent.
    """
    data = client.get_league(["mTransactions2", "mTeam"])

    teams = {
        t["id"]: (t.get("name") or t.get("abbrev") or f"Team {t['id']}")
        for t in data.get("teams", [])
        if t.get("id") is not None
    }

    raw: list[dict] = []
    for txn in data.get("transactions", []):
        if not txn.get("isPending"):
            continue
        if "TRADE" not in (txn.get("type") or ""):
            continue

        proposing_team_id = txn.get("teamId")
        if proposing_team_id == team_id:
            continue  # our own outgoing proposal, not one to respond to

        items = txn.get("items", [])
        offered_to_us = [
            i["playerId"]
            for i in items
            if i.get("toTeamId") == team_id and "playerId" in i
        ]
        requested_from_us = [
            i["playerId"]
            for i in items
            if i.get("fromTeamId") == team_id and "playerId" in i
        ]

        if not offered_to_us and not requested_from_us:
            continue  # doesn't actually involve us

        trade_id = txn.get("id")
        if trade_id is None:
            continue

        raw.append(
            {
                "trade_id": str(trade_id),
                "proposing_team_id": proposing_team_id,
                "offered_to_us": offered_to_us,
                "requested_from_us": requested_from_us,
                "proposed_date": txn.get("proposedDate"),
            }
        )

    # One batched name lookup covering every pending trade, rather than
    # a separate ESPN call per trade — most polls will find zero or one.
    all_player_ids = sorted(
        {
            pid
            for t in raw
            for pid in t["offered_to_us"] + t["requested_from_us"]
        }
    )
    player_info = _player_lookup(client, all_player_ids)

    def _names(ids: list[int]) -> list[str]:
        return [
            player_info.get(pid, {}).get("name", f"ESPN {pid}")
            for pid in ids
        ]

    return [
        PendingIncomingTrade(
            trade_id=t["trade_id"],
            proposing_team_id=t["proposing_team_id"],
            proposing_team_name=teams.get(
                t["proposing_team_id"], f"Team {t['proposing_team_id']}"
            ),
            offered_to_us_ids=t["offered_to_us"],
            offered_to_us_names=_names(t["offered_to_us"]),
            requested_from_us_ids=t["requested_from_us"],
            requested_from_us_names=_names(t["requested_from_us"]),
            proposed_date=t["proposed_date"],
        )
        for t in raw
    ]


def format_trade_side(names: list[str], ids: list[int]) -> str:
    """Shared "Name (id), Name (id)" formatting for CLI/log display."""
    if not ids:
        return "-"
    return ", ".join(f"{name} ({pid})" for name, pid in zip(names, ids))


def _player_lookup(
    client: ESPNClient, player_ids: list[int]
) -> dict[int, dict]:
    if not player_ids:
        return {}

    entries = client.get_players_by_id(player_ids)
    lookup: dict[int, dict] = {}
    for entry in entries:
        player = entry.get("player") or entry
        espn_id = entry.get("id") or player.get("id")
        if espn_id is None:
            continue

        lookup[int(espn_id)] = {
            "name": player.get("fullName") or f"ESPN {espn_id}",
            "position": POSITION_IDS.get(
                player.get("defaultPositionId"),
                str(player.get("defaultPositionId") or "?"),
            ),
            "injury_status": player.get("injuryStatus"),
        }
    return lookup


def evaluate_incoming_trade(
    client: ESPNClient,
    *,
    team_id: int,
    trade: PendingIncomingTrade,
    deep_dive_budget: int = 1,
    attempt_transaction: bool = True,
    confirm: bool = False,
    store: PostgresContextStore | None = None,
    decision_store: IncomingTradeDecisionStore | None = None,
    router: ContextModelRouter | None = None,
) -> IncomingTradeResult:
    """
    Evaluate one specific pending incoming trade and decide accept/
    reject. A single reasoning call, since (unlike the outgoing pipeline)
    there's no candidate search here -- the trade is already fully
    specified by the other team, so this is one yes/no judgment, not a
    multi-stage search-then-decide pipeline.

    Shadows unless confirm=True AND FANTASY_GM_TRANSACTIONS_MODE=live --
    same double-gate pattern as lineup/waiver. This was a deliberate,
    explicit policy change (like waiver's), not a default flip: confirm
    defaults to False, so nothing submits differently until it's set.
    """
    store = store or PostgresContextStore()
    decision_store = decision_store or IncomingTradeDecisionStore()

    if router is None:
        router = ContextModelRouter(
            luna_model=os.getenv("FANTASY_GM_CONTEXT_MODEL", "gpt-5.6-luna"),
            terra_model=os.getenv(
                "FANTASY_GM_DEEP_DIVE_MODEL", "gpt-5.6-terra"
            ),
        )

    reasoning_model = os.getenv("FANTASY_GM_CONTEXT_MODEL", "gpt-5.6-luna")
    calls_used = 0
    cached_context = store.load_all()

    our_roster = load_team_roster(client, team_id)
    roster_payload = _roster_payload(our_roster, cached_context)

    player_ids = trade.offered_to_us_ids + trade.requested_from_us_ids
    player_info = _player_lookup(client, player_ids)

    def _side_payload(ids: list[int]) -> list[dict]:
        payload = []
        for pid in ids:
            info = player_info.get(pid, {})
            ctx = cached_context.get(pid)
            payload.append(
                {
                    "espn_id": pid,
                    "name": info.get("name", f"ESPN {pid}"),
                    "position": info.get("position", "?"),
                    "injury_status": info.get("injury_status"),
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

    try:
        stage1 = call_structured_json(
            model=reasoning_model,
            system=INCOMING_TRADE_SYSTEM.format(
                deep_dive_budget=deep_dive_budget
            ),
            user=(
                f"Proposing team: {trade.proposing_team_name}\n\n"
                f"Our current roster:\n{json.dumps(roster_payload, indent=2)}\n\n"
                f"Players we would RECEIVE:\n"
                f"{json.dumps(_side_payload(trade.offered_to_us_ids), indent=2)}\n\n"
                f"Players we would GIVE UP:\n"
                f"{json.dumps(_side_payload(trade.requested_from_us_ids), indent=2)}\n\n"
                "Return JSON:\n"
                "{\n"
                '  "accept": boolean,\n'
                '  "confidence": number 0 to 1,\n'
                '  "reasoning": "one or two sentences",\n'
                '  "deep_dive_requests": '
                f'[{{"espn_id": int, "name": str, "reason": str}}] '
                f"(at most {deep_dive_budget})\n"
                "}"
            ),
        )
        calls_used += 1

        accept = bool(stage1.get("accept", False))
        confidence = float(stage1.get("confidence", 0.0))
        reasoning = str(stage1.get("reasoning", ""))

        deep_dive_requests = [
            DeepDiveRequest(
                espn_id=int(r["espn_id"]),
                name=str(r.get("name", "")),
                reason=str(r.get("reason", "")),
            )
            for r in stage1.get("deep_dive_requests", [])[:deep_dive_budget]
            if isinstance(r, dict) and "espn_id" in r
        ]

        for request in deep_dive_requests:
            info = player_info.get(request.espn_id, {})
            routed = router.terra.research(
                espn_id=request.espn_id,
                player_name=info.get("name", request.name),
                position=info.get("position", "?"),
                nfl_team=None,
                quantitative_context={"deep_dive_reason": request.reason},
            )
            store.put(routed)
            calls_used += 1

        executed = False
        if attempt_transaction:
            txn = ESPNTransactionsClient(client)
            response = txn.respond_to_trade(
                team_id=team_id,
                trade_id=trade.trade_id,
                accept=accept,
                scoring_period_id=current_scoring_period(client),
                confirm=confirm,
            )
            executed = response is not None

        result = IncomingTradeResult(
            trade=trade,
            accept=accept,
            confidence=confidence,
            reasoning=reasoning,
            deep_dives_used=deep_dive_requests,
            calls_used=calls_used,
            executed=executed,
        )
    except Exception as exc:
        # Record even on failure -- otherwise has_decided() stays False
        # forever and the supervisor's poll loop treats this trade as
        # brand new on every single cycle, re-dispatching a worker that
        # will just fail the same way again, with no backoff at all
        # (this is exactly what happened live: an OpenAI quota error
        # turned into an infinite crash-loop). A failure is recorded as
        # accept=False/executed=False with a distinguishing reasoning
        # message rather than a real judgment, which is enough to stop
        # the automatic retries -- resolving it for real still requires
        # a human, via --force-accept/--force-reject --confirm, or by
        # deleting the row from incoming_trade_decisions to allow one
        # more automatic attempt.
        logger.exception(
            "incoming trade: evaluation failed for %s", trade.trade_id
        )
        failure_result = IncomingTradeResult(
            trade=trade,
            accept=False,
            confidence=0.0,
            reasoning=(
                f"ERROR: automatic evaluation failed and was not "
                f"retried: {exc!r}. Resolve manually with `worker "
                f"respond-trade --trade-id {trade.trade_id} "
                "--force-accept` or `--force-reject` (plus --confirm to "
                "actually submit), or delete this trade's row from "
                "incoming_trade_decisions to allow another automatic "
                "attempt."
            ),
            deep_dives_used=[],
            calls_used=calls_used,
            executed=False,
        )
        try:
            decision_store.record(
                trade=trade, team_id=team_id, result=failure_result
            )
        except Exception:
            logger.exception(
                "incoming trade: failed to record FAILURE decision for %s",
                trade.trade_id,
            )
        raise

    try:
        decision_store.record(trade=trade, team_id=team_id, result=result)
    except Exception:
        logger.exception(
            "incoming trade: failed to record decision for %s",
            trade.trade_id,
        )

    return result


def force_respond_to_trade(
    client: ESPNClient,
    *,
    team_id: int,
    trade: PendingIncomingTrade,
    accept: bool,
    confirm: bool = False,
    decision_store: IncomingTradeDecisionStore | None = None,
) -> IncomingTradeResult:
    """
    Bypass evaluate_incoming_trade()'s reasoning entirely and submit a
    forced accept/reject — for deliberately testing the transaction
    mechanics (payload shape, real submission) in isolation from the
    LLM's judgment, not for real decision-making.
    """
    decision_store = decision_store or IncomingTradeDecisionStore()

    txn = ESPNTransactionsClient(client)
    response = txn.respond_to_trade(
        team_id=team_id,
        trade_id=trade.trade_id,
        accept=accept,
        scoring_period_id=current_scoring_period(client),
        confirm=confirm,
    )
    executed = response is not None

    result = IncomingTradeResult(
        trade=trade,
        accept=accept,
        confidence=1.0,
        reasoning="Manually forced, bypassing evaluation (mechanics test).",
        deep_dives_used=[],
        calls_used=0,
        executed=executed,
    )

    try:
        decision_store.record(trade=trade, team_id=team_id, result=result)
    except Exception:
        logger.exception(
            "incoming trade: failed to record forced decision for %s",
            trade.trade_id,
        )

    return result
