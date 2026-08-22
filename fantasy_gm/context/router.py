from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from fantasy_gm.board.models import BoardPlayer

from .models import AIContextResult
from .researcher import OpenAIContextResearcher


@dataclass(slots=True)
class EscalationDecision:
    score: float
    escalate: bool
    reasons: list[str]


@dataclass(slots=True)
class RoutedResearchResult:
    final: AIContextResult
    luna: AIContextResult
    terra: AIContextResult | None
    escalation: EscalationDecision


class ContextModelRouter:
    """
    Luna-first, Terra-on-demand context research.

    Luna handles broad/high-volume research. Terra is reserved for players whose
    uncertainty, possible impact, or proximity to our decision makes deeper analysis
    worth the extra API cost.
    """

    def __init__(
        self,
        *,
        luna_model: str = "gpt-5.6-luna",
        terra_model: str = "gpt-5.6-terra",
        threshold: float = 0.55,
    ):
        self.luna = OpenAIContextResearcher(model=luna_model)
        self.terra = OpenAIContextResearcher(model=terra_model)
        self.threshold = threshold

    def research(
    self,
    *,
    player: BoardPlayer,
    quantitative_context: dict[str, Any],
    current_pick: int | None = None,
    next_pick: int | None = None,
    board_rank: int | None = None,
    allow_terra: bool = True,
) -> RoutedResearchResult:
        luna_result = self.luna.research(
            espn_id=player.espn_id,
            player_name=player.name,
            position=player.position,
            nfl_team=player.nfl_team_id,
            quantitative_context=quantitative_context,
        )

        decision = self.should_escalate(
            player=player,
            result=luna_result,
            current_pick=current_pick,
            next_pick=next_pick,
            board_rank=board_rank,
        )

        if not decision.escalate or not allow_terra:
            return RoutedResearchResult(
                final=luna_result,
                luna=luna_result,
                terra=None,
                escalation=decision,
            )

        prior = {
            "luna_analysis": {
                "summary": luna_result.summary,
                "direct_delta": luna_result.direct_delta,
                "confidence": luna_result.confidence,
                "category": luna_result.category,
                "availability_risk": luna_result.availability_risk,
                "distraction_risk": luna_result.distraction_risk,
                "role_change": luna_result.role_change,
                "injury_change": luna_result.injury_change,
                "evidence": [asdict(e) for e in luna_result.evidence],
                "related_players": [asdict(r) for r in luna_result.related_players],
            },
            "escalation": {
                "score": decision.score,
                "reasons": decision.reasons,
            },
        }

        terra_context = dict(quantitative_context)
        terra_context["prior_analysis"] = prior
        terra_context["deep_dive_instruction"] = (
            "This is a Terra escalation. Resolve uncertainty and conflicting evidence. "
            "Do not merely restate the Luna result. Search for stronger/primary/current "
            "evidence, identify what Luna may have missed, and return your own final "
            "bounded adjustment."
        )

        terra_result = self.terra.research(
            espn_id=player.espn_id,
            player_name=player.name,
            position=player.position,
            nfl_team=player.nfl_team_id,
            quantitative_context=terra_context,
        )

        return RoutedResearchResult(
            final=terra_result,
            luna=luna_result,
            terra=terra_result,
            escalation=decision,
        )

    def should_escalate(
        self,
        *,
        player: BoardPlayer,
        result: AIContextResult,
        current_pick: int | None = None,
        next_pick: int | None = None,
        board_rank: int | None = None,
    ) -> EscalationDecision:
        score = 0.0
        reasons: list[str] = []

        # 1) Luna itself is uncertain.
        uncertainty = 1.0 - max(0.0, min(1.0, result.confidence))
        score += uncertainty * 0.18
        if result.confidence < 0.68:
            reasons.append(f"low Luna confidence ({result.confidence:.0%})")

        # 2) The possible contextual swing is large.
        impact = min(1.0, abs(result.direct_delta) / 10.0)
        score += impact * 0.18
        if abs(result.direct_delta) >= 4.0:
            reasons.append(f"large context swing ({result.direct_delta:+.1f})")

        # 3) Availability uncertainty is high-value fantasy information.
        score += result.availability_risk * 0.13
        if result.availability_risk >= 0.25:
            reasons.append(f"availability risk ({result.availability_risk:.0%})")

        # 4) Distraction/discipline/legal/contract stories often warrant stronger sourcing.
        score += result.distraction_risk * 0.08
        if result.distraction_risk >= 0.20:
            reasons.append(f"distraction risk ({result.distraction_risk:.0%})")

        # 5) Role change / depth-chart uncertainty can create sleepers.
        role_magnitude = min(1.0, abs(result.role_change))
        score += role_magnitude * 0.12
        if abs(result.role_change) >= 0.40:
            reasons.append(f"material role change ({result.role_change:+.2f})")

        # 6) Teammate propagation can affect more than one asset.
        related_strength = sum(
            abs(r.delta) * max(0.0, min(1.0, r.confidence))
            for r in result.related_players
        )
        related_factor = min(1.0, related_strength / 10.0)
        score += related_factor * 0.08
        if related_strength >= 4.0:
            reasons.append("meaningful teammate propagation")

        # 7) Weak / sparse evidence.
        evidence_count = len(result.evidence)
        avg_reliability = (
            sum(e.reliability for e in result.evidence) / evidence_count
            if evidence_count
            else 0.0
        )
        if evidence_count < 2:
            score += 0.08
            reasons.append("sparse evidence")
        if avg_reliability < 0.65:
            score += 0.07
            reasons.append(f"weak source quality ({avg_reliability:.2f})")

        # 8) A player near an upcoming pick is worth spending on.
        if board_rank is not None:
            if board_rank <= 10:
                score += 0.03
            elif board_rank <= 25:
                score += 0.01

        if next_pick is not None and player.adp is not None:
            distance = abs(player.adp - next_pick)
            if distance <= 8:
                score += 0.10
                reasons.append("ADP near upcoming selection")
            elif distance <= 16:
                score += 0.05

        # 9) Our model and market disagree materially.
        # We use VOR rank proxy vs ECR in service.py when available; here a coarse
        # player-level proxy catches obvious cases.
        if player.ecr is not None and board_rank is not None:
            disagreement = abs(player.ecr - board_rank)
            if disagreement >= 20:
                score += 0.10
                reasons.append(
                    f"our board vs ECR disagreement ({board_rank} vs {player.ecr:.0f})"
                )
            elif disagreement >= 10:
                score += 0.05

        score = max(0.0, min(1.0, score))

        return EscalationDecision(
            score=score,
            escalate=score >= self.threshold,
            reasons=reasons or ["no major escalation signals"],
        )
