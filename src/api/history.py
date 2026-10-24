"""Wording for one event in the live-device history. Pure, no I/O.

The ledger marks an event `counted: false` for three different reasons, and each gets its own label:
a mail scanner's bot-flagged event, an event at or below the row's tier, and a failure (no tier at all).
"""
from typing import Any, Dict, Optional

from common.tiers import rank


def event_note(entry: Dict[str, Any], row_state: str) -> str:
    """Note shown after the event line; empty when the event raised the tier or says everything itself."""
    if entry.get("counted") is not False:
        return ""
    if entry.get("is_bot"):
        return "bot-flagged, not counted"
    event_rank = rank(entry.get("state", ""))
    if event_rank == 0:
        return ""  # a failed event carries no tier: its own state already says failed
    row_rank = rank(row_state)
    if event_rank < row_rank:
        return f"row already at {row_state}"
    if event_rank == row_rank:
        return "already at this tier"
    return "not counted"


def _first_status(raw: Any) -> Optional[Dict[str, Any]]:
    entry = raw.get("whatsAppWebhookEntry") if isinstance(raw, dict) else None
    changes = entry.get("changes") if isinstance(entry, dict) else None
    for change in changes if isinstance(changes, list) else []:
        value = change.get("value") if isinstance(change, dict) else None
        statuses = value.get("statuses") if isinstance(value, dict) else None
        for status in statuses if isinstance(statuses, list) else []:
            if isinstance(status, dict) and isinstance(status.get("status"), str):
                return status
    return None


def whatsapp_status_chip(channel: Optional[str], raw: Any) -> Optional[str]:
    """The WhatsApp `status` value of a stored status event ("read", "failed · error 131031"), or None."""
    status = _first_status(raw) if channel == "whatsapp" else None
    if status is None:
        return None
    errors = status.get("errors")
    code = errors[0].get("code") if isinstance(errors, list) and errors and isinstance(errors[0], dict) else None
    return status["status"] + (f" · error {code}" if code is not None else "")
