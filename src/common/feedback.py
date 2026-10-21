"""Loop closer: PutMessageFeedback RECEIVED on the RCS message id after a tap.

This tells End User Messaging "my user got it" so the sponsor's feedback metrics
move. It is never evidence and never changes the ledger.
Only the RCS channel is closed this way: an acknowledgment on WhatsApp or email
says nothing about the RCS message.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import boto3

from common import config, ledger

log = logging.getLogger(__name__)
WINDOW = timedelta(hours=1)  # End User Messaging fails feedback records left unanswered after one hour


def _parse(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def rcs_sent_entry(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for entry in item.get("tier_history", []):
        if entry.get("channel") == "rcs" and entry.get("state") == "sent" and entry.get("provider_message_id"):
            return entry
    return None


def close_loop(tbl, item: Dict[str, Any], now: Optional[datetime] = None, client=None) -> str:
    """Returns one of: sent, already_sent, no_rcs_message, window_expired, simulated."""
    if item.get("feedback_sent"):
        return "already_sent"
    entry = rcs_sent_entry(item)
    if entry is None:
        return "no_rcs_message"
    if str(entry["provider_message_id"]).startswith("replay-"):
        return "simulated"  # replayed recipients have no real RCS message to give feedback on
    now = now or datetime.now(timezone.utc)
    sent_at = _parse(entry.get("at") or "")
    if sent_at is None or now - sent_at > WINDOW:
        log.warning("PutMessageFeedback skipped for %s: outside the one-hour window", item["recipient_id"])
        return "window_expired"
    client = client or boto3.client("pinpoint-sms-voice-v2", region_name=config.REGION)
    client.put_message_feedback(MessageId=entry["provider_message_id"], MessageFeedbackStatus="RECEIVED")  # CDS call: End User Messaging RCS feedback
    ledger.mark_feedback_sent(tbl, item["notice_id"], item["recipient_id"])
    return "sent"
