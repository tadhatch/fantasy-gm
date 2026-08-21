from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import AIContextResult, EvidenceItem, RelatedPlayerEffect


DEFAULT_PATH = Path(".fantasy-gm/context.json")


class ContextStore:
    def __init__(self, path: Path | str = DEFAULT_PATH):
        self.path = Path(path)

    def load_all(self) -> dict[int, AIContextResult]:
        if not self.path.exists():
            return {}

        data = json.loads(self.path.read_text())
        result: dict[int, AIContextResult] = {}

        for key, row in data.get("players", {}).items():
            result[int(key)] = self._decode(row)

        return result

    def get(self, espn_id: int) -> AIContextResult | None:
        return self.load_all().get(espn_id)

    def put(self, value: AIContextResult) -> None:
        all_rows = self.load_all()
        all_rows[value.espn_id] = value
        self._write(all_rows)

    def put_many(self, values: list[AIContextResult]) -> None:
        all_rows = self.load_all()
        for value in values:
            all_rows[value.espn_id] = value
        self._write(all_rows)

    def is_fresh(self, value: AIContextResult, hours: int = 12) -> bool:
        try:
            dt = datetime.fromisoformat(value.researched_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        return datetime.now(timezone.utc) - dt <= timedelta(hours=hours)

    def _write(self, rows: dict[int, AIContextResult]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "players": {str(k): asdict(v) for k, v in rows.items()},
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        tmp.replace(self.path)

    def _decode(self, row: dict) -> AIContextResult:
        return AIContextResult(
            espn_id=int(row["espn_id"]),
            player_name=row["player_name"],
            researched_at=row["researched_at"],
            model=row["model"],
            summary=row.get("summary", ""),
            direct_delta=float(row.get("direct_delta", 0.0)),
            confidence=float(row.get("confidence", 0.0)),
            category=row.get("category", "news"),
            availability_risk=float(row.get("availability_risk", 0.0)),
            distraction_risk=float(row.get("distraction_risk", 0.0)),
            role_change=float(row.get("role_change", 0.0)),
            injury_change=float(row.get("injury_change", 0.0)),
            evidence=[EvidenceItem(**e) for e in row.get("evidence", [])],
            related_players=[
                RelatedPlayerEffect(**r) for r in row.get("related_players", [])
            ],
            raw_response_id=row.get("raw_response_id"),
        )
