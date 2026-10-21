"""WhatsApp stage. SendWhatsAppMessage passes a Meta Cloud API message object through.

Inside an open 24h customer window we send an interactive reply-button message.
Outside it Meta only accepts an approved template; set WHATSAPP_TEMPLATE_NAME to
use a template with one quick-reply button (the body is its first parameter).
"""
import json
from typing import Any, Dict

from common import config

META_API_VERSION = "v20.0"


def build_message(phone: str, texts: Dict[str, str], postback: str) -> Dict[str, Any]:
    if config.WHATSAPP_TEMPLATE_NAME:
        return {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "template",
            "template": {
                "name": config.WHATSAPP_TEMPLATE_NAME,
                "language": {"code": "en_US"},
                "components": [
                    {"type": "body", "parameters": [{"type": "text", "text": texts["body"][:1024]}]},
                    {"type": "button", "sub_type": "quick_reply", "index": "0",
                     "parameters": [{"type": "payload", "payload": postback}]},
                ],
            },
        }
    return {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": texts["body"][:1024]},
            "action": {"buttons": [{"type": "reply", "reply": {"id": postback, "title": texts["ack_label"][:20]}}]},
        },
    }


def send(client, phone: str, texts: Dict[str, str], postback: str) -> str:
    resp = client.send_whatsapp_message(  # CDS call: End User Messaging Social
        originationPhoneNumberId=config.WHATSAPP_PHONE_NUMBER_ID,
        message=json.dumps(build_message(phone, texts, postback)).encode(),
        metaApiVersion=META_API_VERSION,
    )
    return resp["messageId"]
