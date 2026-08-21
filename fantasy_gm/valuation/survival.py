from __future__ import annotations

import math


def survival_probability(
    *,
    next_pick: int | None,
    adp: float | None,
    ecr: float | None,
    ecr_sd: float | None = None,
) -> float | None:
    """
    Heuristic P(player remains available until our next pick).

    ADP is preferred because it reflects draft behavior; ECR is fallback.
    """
    if next_pick is None:
        return None

    center = adp if adp is not None else ecr
    if center is None:
        return None

    scale = max(3.5, min(12.0, 4.0 + center / 25.0))
    if ecr_sd is not None:
        scale = max(scale, min(15.0, ecr_sd * 0.75))

    z = (next_pick - center) / scale
    probability = 1.0 / (1.0 + math.exp(z))
    return max(0.0, min(1.0, probability))


def urgency_bonus(survival: float | None, max_bonus: float = 8.0) -> float:
    if survival is None:
        return 0.0
    return max_bonus * (1.0 - survival)
