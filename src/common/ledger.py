"""DynamoDB ledger: one item per recipient per notice (F1).

Keys (single table):
  recipient item  pk=notice#<id>   sk=recipient#<id>
  event item      pk=notice#<id>   sk=event#<eid>        verbatim raw event, dedupe key
  unmatched item  pk=notice#<id>   sk=unmatched#<eid>    events we could not correlate
  meta item       pk=notice#<id>   sk=meta
  message map     pk=msg#<provider message id>  sk=map   provider id -> notice/recipient

Rules enforced here, not in the consumers:
  * a duplicate event changes nothing (event item is written with a condition);
  * a tier never regresses (see tiers.next_state);
  * an acknowledgment without a provider event id and raw payload is refused.
"""
import hashlib
import json
import time
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

from common import config
from common.tiers import UNREACHABLE, next_state, rank

MAX_LOCK_RETRIES = 8


def table(resource=None):
    resource = resource or boto3.resource("dynamodb", region_name=config.REGION)
    return resource.Table(config.LEDGER_TABLE)


def notice_pk(notice_id: str) -> str:
    return f"notice#{notice_id}"


def event_id(envelope: Dict[str, Any]) -> str:
    """Deterministic id of one provider event; the dedupe key."""
    basis = "|".join(
        str(envelope.get(k, ""))
        for k in ("channel", "event", "provider_message_id", "provider_timestamp", "dedupe_extra")
    )
    return hashlib.sha256(basis.encode()).hexdigest()[:32]


def put_recipient(tbl, notice_id: str, recipient: Dict[str, Any]) -> None:
    """Create a recipient row in state `queued`-free form: no state until a send is recorded."""
    caps = [c for c in config.LADDER if _has_channel(recipient, c)]
    item = {
        "pk": notice_pk(notice_id),
        "sk": f"recipient#{recipient['recipient_id']}",
        "notice_id": notice_id,
        "recipient_id": recipient["recipient_id"],
        "segment": recipient.get("segment", "consumer"),
        "channel_caps": caps,
        "state": "none",
        "by_channel": {},
        "tier_history": [],
        "stage": 0,
        "unreachable_reason": None,
        "live_device": bool(recipient.get("live_device", False)),
        "feedback_sent": False,
        "version": 0,
        # contact data is kept for the senders; the read API never returns it
        "phone": recipient.get("phone", ""),
        "email": recipient.get("email", ""),
    }
    tbl.put_item(Item=item)


def _has_channel(recipient: Dict[str, Any], channel: str) -> bool:
    if channel == "rcs":
        return bool(recipient.get("rcs_capable"))
    if channel == "whatsapp":
        return bool(recipient.get("whatsapp"))
    if channel == "email":
        return bool(recipient.get("email"))
    return False


def get_recipient(tbl, notice_id: str, recipient_id: str) -> Optional[Dict[str, Any]]:
    resp = tbl.get_item(Key={"pk": notice_pk(notice_id), "sk": f"recipient#{recipient_id}"}, ConsistentRead=True)
    return resp.get("Item")


def register_message(tbl, provider_message_id: str, notice_id: str, recipient_id: str, channel: str) -> None:
    tbl.put_item(
        Item={
            "pk": f"msg#{provider_message_id}",
            "sk": "map",
            "notice_id": notice_id,
            "recipient_id": recipient_id,
            "channel": channel,
        }
    )


def lookup_message(tbl, provider_message_id: str) -> Optional[Dict[str, Any]]:
    if not provider_message_id:
        return None
    resp = tbl.get_item(Key={"pk": f"msg#{provider_message_id}", "sk": "map"})
    return resp.get("Item")


def record_event(tbl, envelope: Dict[str, Any]) -> Dict[str, Any]:
    """Apply one normalized event to the ledger.

    Returns {"status": "applied" | "no_change" | "duplicate" | "unmatched", ...}.
    Raises ValueError for an acknowledgment that cannot be defended.
    """
    notice_id = envelope.get("notice_id")
    recipient_id = envelope.get("recipient_id")
    tier = envelope["tier"]
    eid = event_id(envelope)

    if tier == "acknowledged" and not (envelope.get("provider_message_id") and envelope.get("raw")):
        raise ValueError("refusing acknowledgment without provider event id and raw payload")

    if not (notice_id and recipient_id):
        return _store_unmatched(tbl, envelope, eid, notice_id)

    event_item = {
        "pk": notice_pk(notice_id),
        "sk": f"event#{eid}",
        "recipient_id": recipient_id,
        "channel": envelope["channel"],
        "event": envelope["event"],
        "tier": tier,
        "provider_message_id": envelope.get("provider_message_id"),
        "provider_timestamp": envelope.get("provider_timestamp"),
        "is_bot": bool(envelope.get("is_bot", False)),
        "replayed": bool(envelope.get("replayed", False)),
        "raw_json": json.dumps(envelope.get("raw"), sort_keys=True, default=str),
    }
    try:
        tbl.put_item(Item=event_item, ConditionExpression="attribute_not_exists(pk)")
    except ClientError as err:
        if err.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return {"status": "duplicate", "event_id": eid}
        raise

    for _ in range(MAX_LOCK_RETRIES):
        item = get_recipient(tbl, notice_id, recipient_id)
        if item is None:
            return _store_unmatched(tbl, envelope, eid, notice_id)
        updated, counted = _apply(item, envelope, eid)
        try:
            tbl.put_item(Item=updated, ConditionExpression="version = :v", ExpressionAttributeValues={":v": item["version"]})
        except ClientError as err:
            if err.response["Error"]["Code"] == "ConditionalCheckFailedException":
                continue
            raise
        status = "applied" if counted and updated["state"] != item["state"] else "no_change"
        return {"status": status, "event_id": eid, "state": updated["state"], "previous_state": item["state"]}
    raise RuntimeError(f"ledger write contention on {notice_id}/{recipient_id}")


def _apply(item: Dict[str, Any], envelope: Dict[str, Any], eid: str):
    """Pure-ish: return (new item, whether the event counted toward the tier)."""
    tier = envelope["tier"]
    counted = not envelope.get("is_bot")
    # Bot-flagged events (opens, and clicks by mail scanners) are stored but never counted.
    new_state = next_state(item["state"], tier) if counted else item["state"]
    entry = {
        "state": tier,
        "channel": envelope["channel"],
        "provider_message_id": envelope.get("provider_message_id"),
        "at": envelope.get("provider_timestamp"),
        "event": envelope["event"],
        "event_id": eid,
        "counted": counted and rank(tier) > rank(item["state"]),
        "is_bot": bool(envelope.get("is_bot", False)),
        "replayed": bool(envelope.get("replayed", False)),
    }
    updated = dict(item)
    updated["tier_history"] = list(item.get("tier_history", [])) + [entry]
    updated["state"] = new_state
    by_channel = dict(item.get("by_channel", {}))
    if counted and rank(tier) > rank(by_channel.get(envelope["channel"], "")):
        by_channel[envelope["channel"]] = tier
    updated["by_channel"] = by_channel
    if new_state != UNREACHABLE:
        updated["unreachable_reason"] = None
    updated["version"] = item["version"] + 1
    return updated, counted


def _update_recipient(tbl, notice_id: str, recipient_id: str, change) -> bool:
    """Optimistic-lock read-modify-write. `change(item)` returns the new item or None to skip.

    One helper for every non-event update so the retry rule lives in one place.
    """
    for _ in range(MAX_LOCK_RETRIES):
        item = get_recipient(tbl, notice_id, recipient_id)
        if item is None:
            return False
        updated = change(item)
        if updated is None:
            return False
        updated["version"] = item["version"] + 1
        try:
            tbl.put_item(Item=updated, ConditionExpression="version = :v", ExpressionAttributeValues={":v": item["version"]})
            return True
        except ClientError as err:
            if err.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
    raise RuntimeError(f"ledger write contention on {notice_id}/{recipient_id}")


def record_failure(tbl, notice_id: str, recipient_id: str, channel: str, event: str, reason: str, at: str) -> None:
    """Remember the last failure so 'unreachable' can carry a reason. Never changes the tier."""
    failure = {"channel": channel, "event": event, "reason": reason, "at": at}
    _update_recipient(tbl, notice_id, recipient_id, lambda item: {**item, "last_failure": failure})


def set_stage(tbl, notice_id: str, recipient_id: str, stage: int) -> None:
    _update_recipient(tbl, notice_id, recipient_id, lambda item: {**item, "stage": stage})


def mark_feedback_sent(tbl, notice_id: str, recipient_id: str) -> None:
    _update_recipient(tbl, notice_id, recipient_id, lambda item: {**item, "feedback_sent": True})


def mark_unreachable(tbl, notice_id: str, recipient_id: str, reason: str) -> bool:
    """Set the engine verdict. Never overrides an acknowledgment. Returns True if set."""
    def change(item):
        if item["state"] == "acknowledged":
            return None
        return {**item, "state": UNREACHABLE, "unreachable_reason": reason}

    return _update_recipient(tbl, notice_id, recipient_id, change)


def _store_unmatched(tbl, envelope: Dict[str, Any], eid: str, notice_id: Optional[str]) -> Dict[str, Any]:
    tbl.put_item(
        Item={
            "pk": notice_pk(notice_id or "unknown"),
            "sk": f"unmatched#{eid}",
            "channel": envelope.get("channel"),
            "event": envelope.get("event"),
            "provider_message_id": envelope.get("provider_message_id"),
            "raw_json": json.dumps(envelope.get("raw"), sort_keys=True, default=str),
            "at": envelope.get("provider_timestamp"),
        }
    )
    return {"status": "unmatched", "event_id": eid}


def query_prefix(tbl, notice_id: str, prefix: str, projection: Optional[str] = None, names: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """All items of a notice whose sort key starts with prefix, following pagination."""
    items: List[Dict[str, Any]] = []
    kwargs: Dict[str, Any] = {
        "KeyConditionExpression": "pk = :pk AND begins_with(sk, :p)",
        "ExpressionAttributeValues": {":pk": notice_pk(notice_id), ":p": prefix},
    }
    if projection:
        kwargs["ProjectionExpression"] = projection
    if names:
        kwargs["ExpressionAttributeNames"] = names
    while True:
        resp = tbl.query(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def put_meta(tbl, notice_id: str, meta: Dict[str, Any]) -> None:
    tbl.put_item(Item={"pk": notice_pk(notice_id), "sk": "meta", **meta})


def get_meta(tbl, notice_id: str) -> Optional[Dict[str, Any]]:
    return tbl.get_item(Key={"pk": notice_pk(notice_id), "sk": "meta"}, ConsistentRead=True).get("Item")


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def store_reply(tbl, notice_id: str, recipient_id: str, channel: str, text: str, eid: str, at: str) -> None:
    """Free-text reply, kept for the reply summarizer (F15). Never touches the tier."""
    tbl.put_item(
        Item={
            "pk": notice_pk(notice_id),
            "sk": f"reply#{eid}",
            "recipient_id": recipient_id,
            "channel": channel,
            "text": text,
            "at": at,
        }
    )
