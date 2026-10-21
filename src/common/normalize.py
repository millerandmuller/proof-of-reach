"""Turn raw provider payloads into the normalized event envelope. Pure functions.

Envelope: channel, event, tier, provider_message_id, provider_timestamp, raw,
is_bot, replayed; plus optional notice_id/recipient_id (when the payload carries
the postback string), lookup_id (outbound message id to map back to a row), and for
WhatsApp alt_lookup_ids and lookup_phone as further ways to find the row.

Tier `failed` is not a proof tier; it records a failure reason and never moves a state.
Sources: the AWS End User Messaging (RCS and Social) and SES event documentation.
"""
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from common.tiers import UNPARSED, e164, parse_postback, parse_url_postback


def iso_from_epoch_ms(ms: Any) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def iso_from_epoch_s(s: Any) -> str:
    return datetime.fromtimestamp(int(s), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _envelope(channel: str, event: str, tier: str, message_id: Optional[str], ts: str, raw: Any, **extra) -> Dict[str, Any]:
    env = {
        "channel": channel,
        "event": event,
        "tier": tier,
        "provider_message_id": message_id,
        "provider_timestamp": ts,
        "raw": raw,
        "is_bot": False,
        "replayed": False,
        "lookup_id": message_id,
    }
    env.update(extra)
    return env


# ---------------------------------------------------------------- RCS

_RCS_STATUS_TIER = {
    ("RCS_SENT", None): "sent",
    ("RCS_DELIVERED", "DELIVERED"): "delivered",
    ("RCS_READ", None): "read",
}
_RCS_FAILURE_EVENTS = {"RCS_FAILED", "RCS_TTL_EXPIRED"}
_RCS_FAILURE_STATUSES = {"UNDELIVERABLE", "REJECTED"}


def normalize_rcs_status(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Configuration-set event destination record. None means ignore."""
    event = msg.get("eventType", "")
    status = msg.get("messageStatus")
    mid = msg.get("messageId")
    ts = iso_from_epoch_ms(msg["eventTimestamp"]) if msg.get("eventTimestamp") else ""
    if event == "RCS_FALLEN_BACK_TO_SMS":
        # An SMS was sent instead; nothing says it arrived, so the tier stays `sent`.
        return _envelope("rcs", event, "sent", mid, ts, msg, note="sms_fallback")
    if event in _RCS_FAILURE_EVENTS or (event == "RCS_DELIVERED" and status in _RCS_FAILURE_STATUSES):
        reason = msg.get("messageStatusDescription") or status or event
        return _envelope("rcs", event, "failed", mid, ts, msg, failure_reason=str(reason))
    tier = _RCS_STATUS_TIER.get((event, status)) or _RCS_STATUS_TIER.get((event, None))
    if tier is None:
        return None  # PENDING, QUEUED and other non-proof states
    return _envelope("rcs", event, tier, mid, ts, msg)


def normalize_rcs_inbound(msg: Dict[str, Any], sns_timestamp: str) -> Optional[Dict[str, Any]]:
    """Two-way topic record. Suggestion taps become acknowledgments,
    free text becomes a reply for the summarizer, everything else is ignored."""
    body_raw = msg.get("messageBody", "")
    inbound_id = msg.get("inboundMessageId")
    body: Any = None
    try:
        body = json.loads(body_raw)
    except (TypeError, ValueError):
        body = None
    if isinstance(body, dict) and body.get("type") == "SUGGESTION":
        postback = body.get("postbackData", "")
        url_parsed = parse_url_postback(postback)
        if url_parsed:
            # The OpenUrl suggestion: the card was opened, which is a read and never an acknowledgment.
            env = _envelope("rcs", "SUGGESTION_URL", "read", inbound_id, sns_timestamp, msg)
            env["notice_id"], env["recipient_id"] = url_parsed
            env["lookup_id"] = msg.get("previousPublishedMessageId")
            return env
        parsed = parse_postback(postback)
        if not parsed:
            # only a postback we issued is proof; anything else is kept for inspection and never counted
            return _envelope("rcs", "SUGGESTION_POSTBACK", UNPARSED, inbound_id, sns_timestamp, msg)
        env = _envelope("rcs", "SUGGESTION_POSTBACK", "acknowledged", inbound_id, sns_timestamp, msg)
        env["notice_id"], env["recipient_id"] = parsed
        env["lookup_id"] = msg.get("previousPublishedMessageId")
        return env
    if isinstance(body, dict) and body.get("type"):
        return None  # RCS_TYPING, CONVERSATION_STARTED
    if body_raw:
        return _envelope("rcs", "REPLY", "reply", inbound_id, sns_timestamp, msg,
                         reply_text=str(body_raw), lookup_id=msg.get("previousPublishedMessageId"))
    return None


# ---------------------------------------------------------------- WhatsApp

_WA_STATUS_TIER = {"sent": "sent", "delivered": "delivered", "read": "read"}


def unwrap_whatsapp(sns_message: Dict[str, Any]) -> Dict[str, Any]:
    """The AWS header carries the Meta entry as a JSON string."""
    entry = sns_message.get("whatsAppWebhookEntry")
    return json.loads(entry) if isinstance(entry, str) else (entry or {})


def normalize_whatsapp(sns_message: Dict[str, Any]) -> List[Dict[str, Any]]:
    """All events in one SNS record: status updates, button replies and free text.

    The inbound button_reply shape follows Meta's documented webhook; it is not
    shown on any AWS page and is verified by the spike on the live number.
    """
    entry = unwrap_whatsapp(sns_message)
    out: List[Dict[str, Any]] = []
    # AWS puts its own send id at the top of status events (not of inbound ones); it is what the sender mapped.
    aws_send_id = sns_message.get("messageId")
    for change in entry.get("changes", []):
        value = change.get("value", {})
        for st in value.get("statuses", []):
            ts = iso_from_epoch_s(st["timestamp"]) if st.get("timestamp") else ""
            status = st.get("status", "")
            if status == "failed":
                errors = st.get("errors") or [{}]
                # the title is a fixed Meta phrase; the free-text message can carry a number, so it is never used
                reason = errors[0].get("title") or (f"error {errors[0]['code']}" if errors[0].get("code") else "failed")
                env = _envelope("whatsapp", "failed", "failed", st.get("id"), ts, sns_message, failure_reason=str(reason))
            elif status in _WA_STATUS_TIER:
                env = _envelope("whatsapp", status, _WA_STATUS_TIER[status], st.get("id"), ts, sns_message)
            else:
                continue
            # Four ways back to the row: the postback echoed by Meta, the AWS send id, the phone, and (later) the wamid itself.
            parsed = parse_postback(st.get("biz_opaque_callback_data", ""))
            if parsed:
                env["notice_id"], env["recipient_id"] = parsed
            env["alt_lookup_ids"] = [aws_send_id] if aws_send_id else []
            env["lookup_phone"] = e164(st.get("recipient_id"))
            env["learn_lookup"] = True  # remember the wamid so later statuses match by id
            out.append(env)
        for m in value.get("messages", []):
            ts = iso_from_epoch_s(m["timestamp"]) if m.get("timestamp") else ""
            interactive = m.get("interactive") or {}
            if m.get("type") == "interactive" and interactive.get("type") == "button_reply":
                button_id = (interactive.get("button_reply") or {}).get("id", "")
                parsed = parse_postback(button_id)
                if not parsed:  # a button we did not issue is not an acknowledgment
                    out.append(_envelope("whatsapp", "BUTTON_REPLY", UNPARSED, m.get("id"), ts, sns_message))
                    continue
                env = _envelope("whatsapp", "BUTTON_REPLY", "acknowledged", m.get("id"), ts, sns_message,
                                lookup_id=(m.get("context") or {}).get("id"))
                env["notice_id"], env["recipient_id"] = parsed
                out.append(env)
            elif m.get("type") == "button":
                # Quick-reply button of a template message (fallback path of the WhatsApp stage).
                parsed = parse_postback((m.get("button") or {}).get("payload", ""))
                if not parsed:
                    out.append(_envelope("whatsapp", "BUTTON_REPLY", UNPARSED, m.get("id"), ts, sns_message))
                    continue
                env = _envelope("whatsapp", "BUTTON_REPLY", "acknowledged", m.get("id"), ts, sns_message,
                                lookup_id=(m.get("context") or {}).get("id"))
                env["notice_id"], env["recipient_id"] = parsed
                out.append(env)
            elif m.get("type") == "text":
                out.append(_envelope("whatsapp", "REPLY", "reply", m.get("id"), ts, sns_message,
                                     reply_text=(m.get("text") or {}).get("body", ""),
                                     lookup_id=(m.get("context") or {}).get("id"),
                                     lookup_phone=e164(m.get("from"))))
    return out


# ---------------------------------------------------------------- SES

def normalize_ses(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """SES event publishing record. A click on the acknowledgment link is the acknowledgment."""
    event = msg.get("eventType") or msg.get("notificationType", "")
    mail = msg.get("mail", {})
    mid = mail.get("messageId")
    if event == "Delivery":
        return _envelope("email", event, "delivered", mid, msg["delivery"]["timestamp"], msg)
    if event == "Open":
        op = msg.get("open", {})
        return _envelope("email", event, "read", mid, op.get("timestamp", ""), msg, is_bot=op.get("isBotEvent") == "Likely")
    if event == "Click":
        click = msg.get("click", {})
        parsed = parse_postback(click.get("link", ""))
        if not parsed:
            return None  # a click on some other link is not an acknowledgment
        env = _envelope("email", event, "acknowledged", mid, click.get("timestamp", ""), msg,
                        is_bot=click.get("isBotEvent") == "Likely")
        env["notice_id"], env["recipient_id"] = parsed
        return env
    if event in ("Bounce", "Reject", "Complaint"):
        detail = msg.get(event.lower(), {})
        reason = detail.get("bounceType") and f"{detail['bounceType']}/{detail.get('bounceSubType')}" or detail.get("reason") or ""
        ts = detail.get("timestamp") or mail.get("timestamp", "")
        return _envelope("email", event, "failed", mid, ts, msg, failure_reason=f"{event.lower()} {reason}")
    return None  # Send, DeliveryDelay, Subscription, Rendering Failure
