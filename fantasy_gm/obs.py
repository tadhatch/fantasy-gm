# fantasy_gm/obs.py
#
# Structured logging helpers. Every decision-relevant event (worker
# dispatch, availability validation, lineup submission, ...) is logged
# as a single line: `event=<name> key=value key=value`, so Railway's log
# search can answer "why did this happen" by grepping one event name
# instead of reconstructing it from free-text messages.

from __future__ import annotations

import json
import logging
import os

_configured = False


def configure_logging() -> None:
    """
    Idempotent root logging setup for CLI entrypoints. Without this,
    every logger.info() in a worker is silently dropped (Python's
    last-resort handler only emits WARNING and above), so decisions
    like "waiver: candidate rejected" never reached Railway's logs.
    """
    global _configured
    if _configured:
        return

    level_name = (os.getenv("FANTASY_GM_LOG_LEVEL", "").strip() or "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level_name, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    _configured = True


def _format_value(value: object) -> str:
    if isinstance(value, str):
        return json.dumps(value) if (" " in value or not value) else value
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, default=str, separators=(",", ":"))
    return str(value)


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    **fields: object,
) -> None:
    parts = [f"event={event}"]
    parts += [f"{key}={_format_value(value)}" for key, value in fields.items()]
    logger.log(level, " ".join(parts))
