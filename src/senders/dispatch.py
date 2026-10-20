"""Send one rung of the ladder to one recipient and record it in the ledger.

Live recipients get a real CDS call. Replayed recipients (live_device false) get
no call: the sent event is recorded with a `replay-` message id and raw
{"simulated": true}, and recorded provider events arrive later through the real
SNS topics and consumers. Nothing simulated is ever recorded as a provider fact
beyond the send itself.
"""
from typing import Any, Dict

import boto3

from common import config, ledger
from common.tiers import build_postback
from senders import email as email_sender
from senders import rcs as rcs_sender
from senders import whatsapp as whatsapp_sender


def default_clients() -> Dict[str, Any]:
    return {
        "rcs": boto3.client("pinpoint-sms-voice-v2", region_name=config.REGION),
        "whatsapp": boto3.client("socialmessaging", region_name=config.REGION),
        "email": boto3.client("sesv2", region_name=config.REGION),
    }


def texts_for(meta: Dict[str, Any], segment: str, channel: str) -> Dict[str, str]:
    """Per-segment, per-channel text from the notice meta (falls back to the consumer segment)."""
    by_segment = meta["texts"].get(segment) or meta["texts"]["consumer"]
    texts = dict(by_segment[channel])
    texts["ack_label"] = meta.get("ack_label", "I have read this")
    return texts


def send_rung(tbl, meta: Dict[str, Any], item: Dict[str, Any], channel: str, stage: int, clients: Dict[str, Any]) -> str:
    notice_id, recipient_id = item["notice_id"], item["recipient_id"]
    postback = build_postback(notice_id, recipient_id)
    texts = texts_for(meta, item["segment"], channel)

    if item["live_device"]:
        if channel == "rcs":
            message_id = rcs_sender.send(clients["rcs"], item["phone"], texts, postback, meta.get("notice_url", ""))
        elif channel == "whatsapp":
            message_id = whatsapp_sender.send(clients["whatsapp"], item["phone"], texts, postback)
        else:
            message_id = email_sender.send(clients["email"], item["email"], texts, postback)
        raw = {"MessageId": message_id, "api": "live"}
    else:
        message_id = f"replay-{channel}-{recipient_id}"
        raw = {"MessageId": message_id, "simulated": True}

    ledger.register_message(tbl, message_id, notice_id, recipient_id, channel)
    ledger.record_event(tbl, {
        "notice_id": notice_id, "recipient_id": recipient_id, "channel": channel, "event": "SENT", "tier": "sent",
        "provider_message_id": message_id, "provider_timestamp": ledger.now_iso(), "raw": raw,
        "replayed": not item["live_device"],
    })
    ledger.set_stage(tbl, notice_id, recipient_id, stage)
    return message_id
