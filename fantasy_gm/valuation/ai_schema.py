"""
Structured contract for a future LLM-backed context analyzer.

The LLM should NEVER directly mutate player scores. It proposes a bounded
ContextAdjustment that the deterministic engine applies.

Expected JSON:

{
  "player_id": 123,
  "category": "injury",
  "delta": -8.5,
  "confidence": 0.88,
  "reason": "Expected to miss 3-4 games with a knee injury.",
  "propagation": [
    {
      "relationship": "direct_backup",
      "direction": "positive",
      "strength": 0.75,
      "reason": "Backup is expected to inherit early-down and goal-line work."
    }
  ],
  "evidence_quality": {
    "source_count": 3,
    "official_source_present": true,
    "conflicting_reports": false
  }
}

Recommended hard constraints:
- delta must be between -20 and +20
- confidence must be 0..1
- allegations/off-field events are scored only for distraction/availability impact
- never infer guilt, diagnosis, or undisclosed facts
- preserve article/source URLs for auditability
"""
