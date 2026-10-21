"""Allowlist redaction of raw provider events before they leave the API.

The dashboard shows the raw AWS event next to each live device, and the API has no login.
Provider payloads carry phone numbers and mailbox addresses (originationNumber, recipient_id,
mail.destination, ...), so only keys on an allowlist survive; everything else is dropped.
New provider fields are therefore private until someone decides they are safe.
"""
import json
from typing import Any

ALLOWED_KEYS = {
    # RCS status and inbound events
    "eventType", "eventVersion", "eventTimestamp", "isFinal", "messageId", "messageStatus", "messageStatusDescription",
    "rcsBusinessId", "agentIsoCountryCode", "inboundMessageId", "previousPublishedMessageId", "type", "postbackData",
    # WhatsApp (AWS header + Meta entry)
    "whatsAppWebhookEntry", "message_timestamp", "id", "changes", "field", "value", "messaging_product", "statuses",
    "messages", "status", "timestamp", "context", "interactive", "button_reply", "button", "payload", "title", "errors", "code",
    # SES
    "mail", "delivery", "open", "click", "bounce", "link", "isBotEvent", "bounceType", "bounceSubType",
}


def _filter(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _filter(v) for k, v in value.items() if k in ALLOWED_KEYS}
    if isinstance(value, list):
        return [_filter(v) for v in value]
    return value


def public_raw(raw: Any) -> Any:
    """A copy of a stored raw event that is safe to publish."""
    if not isinstance(raw, dict):
        return None
    raw = dict(raw)
    entry = raw.get("whatsAppWebhookEntry")
    if isinstance(entry, str):  # the AWS header carries the Meta entry as a JSON string
        try:
            raw["whatsAppWebhookEntry"] = json.loads(entry)
        except ValueError:
            raw.pop("whatsAppWebhookEntry")
    body = raw.get("messageBody")
    if isinstance(body, str):  # RCS inbound: a tap carries JSON, a free-text reply carries the customer's own words
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict) and parsed.get("type"):
            raw["messageBody"] = {k: parsed[k] for k in ("type", "postbackData") if k in parsed}
        else:
            raw["messageBody"] = "[reply text omitted]"
    allowed = _filter(raw)
    if "messageBody" in raw:
        allowed["messageBody"] = raw["messageBody"]
    return allowed
