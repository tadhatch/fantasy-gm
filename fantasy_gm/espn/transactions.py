# fantasy_gm/espn/transactions.py
#
# ESPN's roster/waiver/trade transaction API is undocumented and reverse
# engineered from browser network captures (community write-ups such as
# mkreiser/ESPN-Fantasy-Football-API). The endpoint host, payload field
# names, and required fields below are a best-effort reconstruction and
# have NOT been verified against a live league from this codebase.
#
# Before ever running this in "live" mode against a real team:
#   1. Perform each action once by hand on espn.com with browser devtools
#      open (Network tab), capture the exact POST body ESPN's own frontend
#      sends, and diff it against `_build_*` below.
#   2. Fix any field mismatches.
#   3. Only then flip FANTASY_GM_TRANSACTIONS_MODE to "live".
#
# Until that verification happens, every method here defaults to shadow
# mode: it logs the payload it *would* send and returns without calling
# ESPN.

from __future__ import annotations

import os
from dataclasses import dataclass

from rich.console import Console

from fantasy_gm.espn.client import ESPNClient, ESPNError


console = Console()

TRANSACTIONS_WRITE_HOST = "https://lm-api-writes.fantasy.espn.com"


@dataclass(frozen=True, slots=True)
class TransactionsSettings:
    mode: str = "shadow"  # "shadow" or "live"

    @classmethod
    def from_env(cls) -> "TransactionsSettings":
        return cls(
            mode=os.getenv("FANTASY_GM_TRANSACTIONS_MODE", "shadow")
            .strip()
            .lower(),
        )


@dataclass(frozen=True, slots=True)
class LineupMove:
    player_id: int
    from_slot_id: int
    to_slot_id: int


class TransactionRejected(ESPNError):
    pass


class ESPNTransactionsClient:
    """
    Writes roster/waiver/trade transactions to ESPN.

    `confirm=True` is required on every call in addition to live mode being
    enabled — this is a deliberate double gate so a scheduled worker can
    never execute against a real roster until both the global mode AND the
    specific call site have been switched on.
    """

    def __init__(
        self,
        client: ESPNClient,
        *,
        settings: TransactionsSettings | None = None,
    ):
        self.client = client
        self.settings = settings or TransactionsSettings.from_env()

    @property
    def _transactions_url(self) -> str:
        return (
            f"{TRANSACTIONS_WRITE_HOST}/apis/v3/games/ffl/"
            f"seasons/{self.client.settings.espn_season}/segments/0/"
            f"leagues/{self.client.league_id}/transactions/"
        )

    def _post(self, body: dict, *, confirm: bool, label: str) -> dict | None:
        live = self.settings.mode == "live"

        if not live or not confirm:
            reason = (
                "mode=shadow" if not live else "confirm=False"
            )
            console.print(
                f"[yellow][TRANSACTIONS][/yellow] "
                f"shadow ({reason}) — would POST {label}:\n{body}"
            )
            return None

        response = self.client.session.post(
            self._transactions_url,
            json=body,
            timeout=20,
        )

        if response.status_code in (401, 403):
            raise ESPNError(
                f"ESPN authentication failed ({response.status_code}) "
                "submitting transaction"
            )

        if not response.ok:
            raise TransactionRejected(
                f"ESPN rejected {label} "
                f"({response.status_code}): {response.text[:500]}"
            )

        console.print(
            f"[green][TRANSACTIONS][/green] {label} submitted"
        )

        try:
            return response.json()
        except ValueError:
            return None

    def set_lineup(
        self,
        *,
        team_id: int,
        moves: list[LineupMove],
        scoring_period_id: int,
        confirm: bool = False,
    ) -> dict | None:
        body = {
            "isLeagueManager": False,
            "teamId": team_id,
            "type": "ROSTER",
            "memberId": self.client.settings.espn_swid,
            "scoringPeriodId": scoring_period_id,
            "executionType": "EXECUTE",
            "items": [
                {
                    "playerId": move.player_id,
                    "type": "LINEUP",
                    "fromLineupSlotId": move.from_slot_id,
                    "toLineupSlotId": move.to_slot_id,
                }
                for move in moves
            ],
        }

        return self._post(
            body,
            confirm=confirm,
            label=f"lineup set ({len(moves)} moves, team {team_id})",
        )

    def add_drop(
        self,
        *,
        team_id: int,
        add_player_id: int | None,
        drop_player_id: int | None,
        scoring_period_id: int,
        via_waiver: bool = False,
        bid_amount: int | None = None,
        confirm: bool = False,
    ) -> dict | None:
        if add_player_id is None and drop_player_id is None:
            raise ValueError("add_drop requires at least one of add/drop")

        items = []

        if add_player_id is not None:
            item = {
                "playerId": add_player_id,
                "type": "ADD",
                "toTeamId": team_id,
            }
            if via_waiver and bid_amount is not None:
                item["bidAmount"] = bid_amount
            items.append(item)

        if drop_player_id is not None:
            items.append(
                {
                    "playerId": drop_player_id,
                    "type": "DROP",
                    "fromTeamId": team_id,
                }
            )

        body = {
            "isLeagueManager": False,
            "teamId": team_id,
            "type": "WAIVER" if via_waiver else "FREEAGENT",
            "memberId": self.client.settings.espn_swid,
            "scoringPeriodId": scoring_period_id,
            "executionType": "EXECUTE",
            "items": items,
        }

        label = (
            f"add {add_player_id or '-'} / drop {drop_player_id or '-'} "
            f"(team {team_id}, {'waiver' if via_waiver else 'free agent'})"
        )

        return self._post(body, confirm=confirm, label=label)

    def propose_trade(
        self,
        *,
        proposing_team_id: int,
        receiving_team_id: int,
        players_offered: list[int],
        players_requested: list[int],
        scoring_period_id: int,
        message: str = "",
        confirm: bool = False,
    ) -> dict | None:
        # Real transactions of every other type this codebase has pulled
        # from mTransactions2 (ROSTER/LINEUP moves, WAIVER ADD/DROP) carry
        # a full item shape: playerId, type, fromTeamId, toTeamId,
        # fromLineupSlotId, toLineupSlotId, isKeeper, overallPickNumber.
        # The original propose_trade() only sent playerId/type/fromTeamId/
        # toTeamId and was confirmed live to fail with ESPN's generic
        # "Invalid Input" (400) -- this fills in the missing fields to
        # match that real shape, since a leaner set works for LINEUP
        # moves (confirmed live) but apparently isn't accepted for TRADE.
        # fromLineupSlotId is the player's real current slot (looked up
        # live); toLineupSlotId defaults to BENCH (20) for the acquiring
        # team, same as this project's own lineup optimizer's constant --
        # a real acquisition needs a follow-up lineup run regardless of
        # what slot ESPN records it landing in here. Still an inference
        # from other transaction types, not a real browser capture of a
        # trade proposal specifically -- flag it to the user if this
        # still gets rejected.
        BENCH_SLOT_ID = 20

        from fantasy_gm.espn.roster import load_all_rosters

        rosters = load_all_rosters(self.client)
        current_slot: dict[int, int] = {}
        for roster in rosters.values():
            for entry in roster.entries:
                current_slot[entry.player_id] = entry.lineup_slot_id

        items = [
            {
                "playerId": player_id,
                "type": "TRADE",
                "fromTeamId": proposing_team_id,
                "toTeamId": receiving_team_id,
                "fromLineupSlotId": current_slot.get(
                    player_id, BENCH_SLOT_ID
                ),
                "toLineupSlotId": BENCH_SLOT_ID,
                "isKeeper": False,
                "overallPickNumber": 0,
            }
            for player_id in players_offered
        ] + [
            {
                "playerId": player_id,
                "type": "TRADE",
                "fromTeamId": receiving_team_id,
                "toTeamId": proposing_team_id,
                "fromLineupSlotId": current_slot.get(
                    player_id, BENCH_SLOT_ID
                ),
                "toLineupSlotId": BENCH_SLOT_ID,
                "isKeeper": False,
                "overallPickNumber": 0,
            }
            for player_id in players_requested
        ]

        body = {
            "isLeagueManager": False,
            "teamId": proposing_team_id,
            "type": "TRADE_PROPOSAL",
            "memberId": self.client.settings.espn_swid,
            "scoringPeriodId": scoring_period_id,
            "executionType": "EXECUTE",
            "message": message,
            "items": items,
        }

        label = (
            f"trade proposal team {proposing_team_id} -> "
            f"{receiving_team_id} "
            f"(offer {players_offered}, request {players_requested})"
        )

        return self._post(body, confirm=confirm, label=label)

    def respond_to_trade(
        self,
        *,
        team_id: int,
        trade_id: str,
        accept: bool,
        scoring_period_id: int,
        confirm: bool = False,
    ) -> dict | None:
        body = {
            "isLeagueManager": False,
            "teamId": team_id,
            "type": "TRADE_ACCEPT" if accept else "TRADE_REJECT",
            "memberId": self.client.settings.espn_swid,
            "scoringPeriodId": scoring_period_id,
            "executionType": "EXECUTE",
            "relatedTransactionId": trade_id,
        }

        label = (
            f"trade {'accept' if accept else 'reject'} "
            f"{trade_id} (team {team_id})"
        )

        return self._post(body, confirm=confirm, label=label)
