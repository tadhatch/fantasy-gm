from __future__ import annotations

from dataclasses import dataclass

from fantasy_gm.board.models import BoardPlayer
from fantasy_gm.valuation.survival import survival_probability


@dataclass(slots=True)
class ReplacementLoss:
    survival: float | None
    gone_probability: float
    expected_replacement_score: float | None
    raw_gap: float
    expected_loss: float
    replacement_name: str | None = None


def expected_replacement_loss(
    candidate: BoardPlayer,
    *,
    available_board: list[BoardPlayer],
    next_pick: int | None,
    cap: float = 10.0,
) -> ReplacementLoss:
    """
    Estimate the fantasy cost of waiting until our next pick.

    Instead of:
        "Candidate is 95% likely to be gone, therefore +big urgency"

    we calculate:
        P(candidate gone)
        ×
        value(candidate - expected best same-position replacement)

    This naturally suppresses urgency for replaceable positions such as K/DST while
    preserving urgency when the next likely RB/WR/TE tier is much worse.
    """
    survival = survival_probability(
        next_pick=next_pick,
        adp=candidate.adp,
        ecr=candidate.ecr,
        ecr_sd=candidate.ecr_sd,
    )

    if survival is None:
        return ReplacementLoss(
            survival=None,
            gone_probability=0.0,
            expected_replacement_score=None,
            raw_gap=0.0,
            expected_loss=0.0,
        )

    gone_probability = 1.0 - survival

    same_position = [
        p
        for p in available_board
        if p.espn_id != candidate.espn_id
        and p.position == candidate.position
    ]

    if not same_position:
        # No replacement on our board means losing the candidate is materially bad,
        # but keep it bounded.
        gap = min(cap, max(0.0, candidate.board_score))
        return ReplacementLoss(
            survival=survival,
            gone_probability=gone_probability,
            expected_replacement_score=0.0,
            raw_gap=gap,
            expected_loss=min(cap, gone_probability * gap),
        )

    # Highest-value alternatives first.
    same_position.sort(
        key=lambda p: p.board_score,
        reverse=True,
    )

    expected_score = 0.0
    probability_no_better_survivor = 1.0
    representative_name: str | None = None

    # Approximate expected BEST available replacement.
    #
    # For each alternative in value order:
    #   probability it is our best surviving option =
    #       P(this player survives)
    #       × P(all better alternatives are gone)
    #
    # Independence is only an approximation, but this is much more informative
    # than treating ADP survival as value by itself.
    for alt in same_position[:12]:
        alt_survival = survival_probability(
            next_pick=next_pick,
            adp=alt.adp,
            ecr=alt.ecr,
            ecr_sd=alt.ecr_sd,
        )

        if alt_survival is None:
            # Unknown market survival: use a neutral 50% rather than pretending
            # certainty in either direction.
            alt_survival = 0.50

        best_probability = (
            probability_no_better_survivor
            * alt_survival
        )

        expected_score += (
            best_probability
            * alt.board_score
        )

        if representative_name is None and alt_survival >= 0.50:
            representative_name = alt.name

        probability_no_better_survivor *= (
            1.0 - alt_survival
        )

        if probability_no_better_survivor < 0.01:
            break

    # If every modeled alternative disappears, use the lowest modeled score as a
    # conservative fallback instead of letting the expectation collapse to zero.
    modeled = same_position[:12]
    if modeled and probability_no_better_survivor > 0:
        fallback = modeled[-1]
        expected_score += (
            probability_no_better_survivor
            * fallback.board_score
        )
        if representative_name is None:
            representative_name = fallback.name

    raw_gap = max(
        0.0,
        candidate.board_score - expected_score,
    )

    expected_loss = min(
        cap,
        gone_probability * raw_gap,
    )

    return ReplacementLoss(
        survival=survival,
        gone_probability=gone_probability,
        expected_replacement_score=expected_score,
        raw_gap=raw_gap,
        expected_loss=expected_loss,
        replacement_name=representative_name,
    )
