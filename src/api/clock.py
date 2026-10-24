"""The rule's clock for one device: which rung runs next and when it is due. Pure, no I/O.

The due time is the engine's own arithmetic: the time the last stage was recorded plus the
notice's window, the same window rule_engine.schedule_stage uses for the next one-time schedule.
Nothing here predicts; the dashboard counts down to this timestamp and says "sending" once it passes.
"""
from datetime import timedelta
from typing import Any, Dict, Optional

from api.report import _parse
from common import config
from common.tiers import UNREACHABLE
from engine import ladder

ISO = "%Y-%m-%dT%H:%M:%SZ"


def next_step(item: Dict[str, Any], meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The device's next scheduled step, or None when no clock applies (not running, acknowledged, or already unreachable).

    `verdict` is true when no rung is left for this device: the next stage only closes its ladder, and
    `ends_as` names the result the engine's own rule gives it ("unreachable", or the tier it stays at).
    `tried` lists the channels where a send was recorded for this device; `unsent` (verdict only) the rungs that never were.
    `sent_here` is true while the stage is still running over other recipients but this device's own send
    is already recorded. `due_at` is None until stage 1 has been recorded, which the dashboard shows as "sending".
    """
    if meta.get("status") != "running" or item["state"] in ("acknowledged", UNREACHABLE):
        return None
    recorded = int(meta.get("stage", 0))
    stage = recorded + 1
    window = int(meta.get("window_seconds", config.WINDOW_SECONDS))
    stage_at = _parse(meta.get("stage_at")) if recorded else None
    channel = ladder.rung_for(item, stage)
    caps = item.get("channel_caps", [])
    sent = [ch for ch in caps if ch in (item.get("by_channel") or {})]  # a recorded send, not the stage counter: a failed send is not "tried"
    return {
        "next_stage": stage,
        "next_channel": channel,
        "verdict": channel is None,
        "ends_as": (UNREACHABLE if ladder.needs_verdict(item, stage) else item["state"]) if channel is None else None,
        "sent_here": channel is not None and int(item.get("stage", 0)) >= stage,
        "tried": sent,
        "unsent": [ch for ch in caps if ch not in sent] if channel is None else [],  # at the end of the ladder: rungs with no recorded send
        "window_seconds": window,
        "due_at": (stage_at + timedelta(seconds=window)).strftime(ISO) if stage_at else None,
    }
