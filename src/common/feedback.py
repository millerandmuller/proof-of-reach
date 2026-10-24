"""Loop closer: PutMessageFeedback RECEIVED on the RCS message id after a tap.

This tells End User Messaging "my user got it" so the sponsor's feedback metrics
move. It is never evidence and never changes the acknowledgment.
Only the RCS channel is closed this way: an acknowledgment on WhatsApp or email
says nothing about the RCS message.

Live finding (2026-10-24): for a real RCS send AWS answered ResourceNotFoundException, so the call
cannot be relied on. It is attempted once per recipient and the outcome (sent, or the AWS error code)
is stored on the row as `feedback_status`; it is never retried.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

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
    """Returns one of: sent, already_sent, already_attempted, no_rcs_message, window_expired, simulated, failed:<code>.

    Runs after the acknowledgment is committed, so an AWS error is recorded on the row and returned,
    never raised: raising would fail the SQS batch and the redelivery would find a duplicate and skip this step.
    """
    if item.get("feedback_sent"):
        return "already_sent"
    if item.get("feedback_status"):
        return "already_attempted"  # one attempt only, whatever its outcome
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
    try:
        client.put_message_feedback(MessageId=entry["provider_message_id"], MessageFeedbackStatus="RECEIVED")  # CDS call: End User Messaging RCS feedback
    except (ClientError, BotoCoreError) as err:
        code = err.response.get("Error", {}).get("Code", "ClientError") if isinstance(err, ClientError) else "transport:" + type(err).__name__  # transport: no AWS response
        log.error("PutMessageFeedback failed for %s (message %s): %s", item["recipient_id"], entry["provider_message_id"], code)
        ledger.mark_feedback(tbl, item["notice_id"], item["recipient_id"], code)
        return f"failed:{code}"
    ledger.mark_feedback(tbl, item["notice_id"], item["recipient_id"], "sent")
    return "sent"
