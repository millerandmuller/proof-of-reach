"""Pure ladder rules (F6): which rung a recipient gets at which stage. No I/O.

Stage k (1-based) sends rung k of the recipient's own ladder, the channels they
have, in the fixed order rcs, whatsapp, email. Stage len(LADDER)+1 is the close:
everyone not acknowledged is marked unreachable with a reason. An acknowledged
recipient never receives anything again.
"""
from typing import Any, Dict, Optional

from common import config
from common.tiers import UNREACHABLE

CLOSE_STAGE = len(config.LADDER) + 1


def rung_for(item: Dict[str, Any], stage: int) -> Optional[str]:
    """Channel to send at this stage, or None (acknowledged, or the ladder is used up)."""
    if item["state"] == "acknowledged" or stage >= CLOSE_STAGE:
        return None
    caps = item.get("channel_caps", [])
    return caps[stage - 1] if stage - 1 < len(caps) else None


def unreachable_reason(item: Dict[str, Any], stage: int) -> str:
    """Reason string for the exception list. Uses the last provider failure when there is one."""
    failure = item.get("last_failure")
    caps = item.get("channel_caps", [])
    if not caps:
        return "no channel on file"
    tried = ", ".join(caps[: max(stage - 1, 0)] or caps)
    if failure:
        return f"{failure['channel']} {failure['reason']}; no acknowledgment after {tried}"
    return f"no acknowledgment after {tried}"


def needs_verdict(item: Dict[str, Any], stage: int) -> bool:
    """True when this stage ends the recipient's journey without proof."""
    if item["state"] in ("acknowledged", UNREACHABLE):
        return False
    return rung_for(item, stage) is None
