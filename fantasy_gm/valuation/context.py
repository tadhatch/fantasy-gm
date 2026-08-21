from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from .models import AdjustmentCategory, ContextAdjustment, Evidence


@dataclass(slots=True)
class NewsEvent:
    player_id: int
    player_name: str
    headline: str
    body: str
    source: str
    published_at: str | None = None
    url: str | None = None


class ContextModel(Protocol):
    def analyze(self, event: NewsEvent) -> ContextAdjustment | None: ...


class RuleBasedContextModel:
    """
    Safe fallback / test implementation.

    The production AI implementation should return this same structured schema.
    """

    def analyze(self, event: NewsEvent) -> ContextAdjustment | None:
        text = f"{event.headline} {event.body}".lower()

        if any(x in text for x in ("out for", "miss ", "torn acl", "ir ", "injured reserve")):
            category = AdjustmentCategory.INJURY
            delta = -12.0
            confidence = 0.9
            reason = "Material expected missed time reported."
        elif any(x in text for x in ("suspended", "suspension")):
            category = AdjustmentCategory.SUSPENSION
            delta = -10.0
            confidence = 0.9
            reason = "Reported suspension affects expected availability."
        elif any(x in text for x in ("holdout", "holding out", "contract dispute")):
            category = AdjustmentCategory.CONTRACT
            delta = -4.0
            confidence = 0.65
            reason = "Contract situation may affect preparation or availability."
        elif any(x in text for x in ("arrested", "allegation", "accused", "investigation")):
            category = AdjustmentCategory.OFF_FIELD
            delta = -3.0
            confidence = 0.45
            reason = (
                "Reported off-field matter may create distraction or availability risk; "
                "no inference about guilt is made."
            )
        elif any(x in text for x in ("named starter", "earned starting", "first-team reps")):
            category = AdjustmentCategory.ROLE
            delta = 5.0
            confidence = 0.7
            reason = "Reported role improvement."
        else:
            return None

        evidence = Evidence(
            source=event.source,
            title=event.headline,
            published_at=event.published_at,
            url=event.url,
            summary=event.body[:500],
        )

        return ContextAdjustment(
            player_id=event.player_id,
            category=category,
            delta=delta,
            confidence=confidence,
            reason=reason,
            evidence=[evidence],
        )
