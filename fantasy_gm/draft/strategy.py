from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from fantasy_gm.board.models import BoardPlayer
from .replacement import expected_replacement_loss

@dataclass(slots=True)
class RosterRules:
    roster_size: int = 15
    league_size: int = 14
    starters: dict[str, int] = field(default_factory=lambda: {
        "QB": 1,
        "RB": 2,
        "WR": 3,
        "TE": 1,
        "DST": 1,
        "K": 1,
    })
    flex_slots: int = 1
    flex_positions: tuple[str, ...] = ("RB", "WR", "TE")
    max_roster: dict[str, int] = field(default_factory=lambda: {
        "QB": 3,
        "RB": 8,
        "WR": 8,
        "TE": 4,
        "DST": 2,
        "K": 2,
    })


@dataclass(slots=True)
class StrategyBreakdown:
    player_id: int
    base_score: float
    starter_need: float = 0.0
    flex_need: float = 0.0
    scarcity: float = 0.0
    saturation: float = 0.0
    bench_value: float = 0.0
    late_position: float = 0.0
    replacement_loss: float = 0.0
    legality: float = 0.0
    final_score: float = 0.0
    reasons: list[str] = field(default_factory=list)


class RosterStrategy:
    def __init__(self, rules: RosterRules | None = None):
        self.rules = rules or RosterRules()

    def counts(self, our_roster: list[int], player_by_id: dict[int, BoardPlayer]) -> Counter[str]:
        counts: Counter[str] = Counter()
        for player_id in our_roster:
            player = player_by_id.get(player_id)
            if player is not None:
                counts[player.position] += 1
            elif player_id < 0:
                counts["DST"] += 1
        return counts

    def score(
        self,
        player: BoardPlayer,
        *,
        our_roster: list[int],
        player_by_id: dict[int, BoardPlayer],
        available_board: list[BoardPlayer],
        overall_pick: int,
        next_pick: int | None,
    ) -> StrategyBreakdown:
        counts = self.counts(our_roster, player_by_id)
        pos = player.position
        result = StrategyBreakdown(player_id=player.espn_id, base_score=player.board_score)

        max_allowed = self.rules.max_roster.get(pos)
        if max_allowed is not None and counts[pos] >= max_allowed:
            result.legality = -1000.0
            result.final_score = -1000.0
            result.reasons.append(f"{pos} roster maximum reached ({counts[pos]}/{max_allowed})")
            return result

        required = self.rules.starters.get(pos, 0)
        filled = counts[pos]

        if required and filled < required:
            missing = required - filled
            result.starter_need += 8.0 + min(4.0, missing * 1.5)
            result.reasons.append(f"open {pos} starter slot ({filled}/{required})")

        flex_used = self._flex_slots_used(counts)
        if pos in self.rules.flex_positions and flex_used < self.rules.flex_slots and filled >= required:
            result.flex_need += 3.5
            result.reasons.append("can fill open FLEX")

        if pos == "RB":
            result.scarcity += 2.0
        elif pos == "WR":
            result.scarcity += 1.0
        elif pos == "TE" and filled == 0:
            result.scarcity += 1.5

        if pos == "QB":
            if filled == 1:
                result.saturation -= 22.0
                result.reasons.append("elite QB1 already rostered")
            elif filled == 2:
                result.saturation -= 45.0
                result.reasons.append("already carrying 2 QBs")
            elif filled >= 3:
                result.saturation -= 100.0
                result.reasons.append(f"QB room full ({filled})")
        elif pos == "TE":
            if filled == 1:
                result.saturation -= 8.0
                result.reasons.append("TE1 already rostered")
            elif filled >= 2:
                result.saturation -= 22.0
                result.reasons.append(f"TE room saturated ({filled})")
        elif pos in {"RB", "WR"}:
            starter_plus_flex = required + (self.rules.flex_slots if pos in self.rules.flex_positions else 0)
            if filled >= starter_plus_flex:
                result.saturation -= min(12.0, 2.5 * (filled - starter_plus_flex + 1))
                result.reasons.append(f"{pos} depth already strong ({filled})")
        elif pos in {"K", "DST"} and filled >= 1:
            result.saturation -= 100.0
            result.reasons.append(f"{pos} already rostered")

        starters_open = self._starting_slots_open(counts)
        if starters_open <= 2 and pos in {"RB", "WR"}:
            result.bench_value += 2.0
            result.reasons.append("useful RB/WR bench depth")

        roster_count = len(our_roster)
        remaining_after_pick = self.rules.roster_size - (roster_count + 1)
        if pos in {"K", "DST"}:
            if remaining_after_pick >= 4:
                result.late_position -= 30.0
                result.reasons.append(f"too early for {pos}")
            elif remaining_after_pick == 3:
                result.late_position -= 15.0
                result.reasons.append(f"still early for {pos}")
            elif remaining_after_pick <= 2 and counts[pos] == 0:
                result.late_position += 8.0
                result.reasons.append(f"late-round {pos} need")

        loss = expected_replacement_loss(
            player,
            available_board=available_board,
            next_pick=next_pick,
            cap=10.0,
        )

        result.replacement_loss = loss.expected_loss

        if (
            loss.survival is not None
            and loss.expected_loss >= 1.0
        ):
            replacement = (
                f" vs {loss.replacement_name}"
                if loss.replacement_name
                else ""
            )

            result.reasons.append(
                f"wait cost +{loss.expected_loss:.1f}"
                f"{replacement}"
            )

        result.final_score = (
            result.base_score
            + result.starter_need
            + result.flex_need
            + result.scarcity
            + result.saturation
            + result.bench_value
            + result.late_position
            + result.replacement_loss
            + result.legality
        )
        return result

    def rank(
        self,
        board: list[BoardPlayer],
        *,
        taken: set[int],
        our_roster: list[int],
        overall_pick: int,
        next_pick: int | None,
        limit: int = 10,
    ) -> list[tuple[BoardPlayer, StrategyBreakdown]]:
        player_by_id = {p.espn_id: p for p in board}

        available_board = [
            p
            for p in board
            if p.espn_id not in taken
        ]

        scored = []

        for player in available_board:
            breakdown = self.score(
                player,
                our_roster=our_roster,
                player_by_id=player_by_id,
                available_board=available_board,
                overall_pick=overall_pick,
                next_pick=next_pick,
            )
            if breakdown.final_score <= -999:
                continue
            scored.append((player, breakdown))

        scored.sort(key=lambda item: item[1].final_score, reverse=True)
        return scored[:limit]

    def _flex_slots_used(self, counts: Counter[str]) -> int:
        overflow = 0
        for pos in self.rules.flex_positions:
            required = self.rules.starters.get(pos, 0)
            overflow += max(0, counts[pos] - required)
        return min(self.rules.flex_slots, overflow)

    def _starting_slots_open(self, counts: Counter[str]) -> int:
        open_slots = 0
        for pos, required in self.rules.starters.items():
            open_slots += max(0, required - counts[pos])
        open_slots += max(0, self.rules.flex_slots - self._flex_slots_used(counts))
        return open_slots
