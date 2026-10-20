"""Sender payloads are validated against the SDK's own request models via botocore's Stubber."""
import json

import boto3
import pytest
from botocore.stub import Stubber

from common import config, ledger
from common.tiers import build_postback
from senders import dispatch, email as email_sender, rcs, whatsapp

PB = build_postback("HX200-2026-10", "r-1")
TEXTS = {"title": "Heater HX-200 recall", "body": "Heater HX-200 can overheat. Unplug it now. Call 555-0100.",
         "ack_label": "Device off, read", "subject": "Safety notice: heater HX-200"}


def stub_client(service):
    client = boto3.client(service, region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y")
    return client, Stubber(client)


def test_rcs_card_matches_sdk_model_and_carries_postback(monkeypatch):
    monkeypatch.setattr(config, "RCS_AGENT_ID", "rcs-agent-1")
    monkeypatch.setattr(config, "RCS_CONFIGURATION_SET", "por-rcs")
    client, stub = stub_client("pinpoint-sms-voice-v2")
    expected = {
        "DestinationPhoneNumber": "+15550001", "OriginationIdentity": "rcs-agent-1", "ConfigurationSetName": "por-rcs",
        "MessageFeedbackEnabled": True, "RcsMessageContent": rcs.build_content(TEXTS, PB, "https://n.example/notice"),
    }
    stub.add_response("send_rcs_message", {"MessageId": "rcs-msg-1"}, expected)
    with stub:
        assert rcs.send(client, "+15550001", TEXTS, PB, "https://n.example/notice") == "rcs-msg-1"
    reply = expected["RcsMessageContent"]["Content"]["RichCard"]["CardContent"]["Suggestions"][0]["Reply"]
    assert reply["PostbackData"] == PB and len(reply["Text"]) <= 25


def test_rcs_ack_label_truncated_to_25():
    content = rcs.build_content({**TEXTS, "ack_label": "x" * 40}, PB, "")
    assert len(content["Content"]["RichCard"]["CardContent"]["Suggestions"][0]["Reply"]["Text"]) == 25


def test_whatsapp_interactive_message_shape(monkeypatch):
    monkeypatch.setattr(config, "WHATSAPP_PHONE_NUMBER_ID", "phone-number-id-abc")
    monkeypatch.setattr(config, "WHATSAPP_TEMPLATE_NAME", "")
    client, stub = stub_client("socialmessaging")
    sent = {}

    def capture(**kw):
        sent.update(kw)
        return True

    stub.add_response("send_whatsapp_message", {"messageId": "wamid.X"}, None)
    with stub:
        assert whatsapp.send(client, "+15550002", TEXTS, PB) == "wamid.X"
    msg = whatsapp.build_message("+15550002", TEXTS, PB)
    assert msg["type"] == "interactive"
    button = msg["interactive"]["action"]["buttons"][0]["reply"]
    assert button["id"] == PB and len(button["title"]) <= 20


def test_whatsapp_template_fallback_uses_quick_reply_payload(monkeypatch):
    monkeypatch.setattr(config, "WHATSAPP_TEMPLATE_NAME", "recall_notice")
    msg = whatsapp.build_message("+15550002", TEXTS, PB)
    assert msg["type"] == "template"
    assert msg["template"]["components"][1]["parameters"][0]["payload"] == PB


def test_email_link_roundtrips_through_parse_postback(monkeypatch):
    from common.tiers import parse_postback
    monkeypatch.setattr(config, "ACK_BASE_URL", "https://d111.cloudfront.net/")
    monkeypatch.setattr(config, "SES_SENDER", "notice@example.test")
    monkeypatch.setattr(config, "SES_CONFIGURATION_SET", "por-ses")
    assert parse_postback(email_sender.ack_link(PB)) == ("HX200-2026-10", "r-1")
    client, stub = stub_client("sesv2")
    stub.add_response("send_email", {"MessageId": "ses-1"}, {
        "FromEmailAddress": "notice@example.test", "Destination": {"ToAddresses": ["a@example.test"]},
        "ConfigurationSetName": "por-ses",
        "Content": {"Simple": {"Subject": {"Data": TEXTS["subject"]}, "Body": email_sender.build_body(TEXTS, PB)}},
    })
    with stub:
        assert email_sender.send(client, "a@example.test", TEXTS, PB) == "ses-1"


META = {"texts": {"consumer": {"rcs": {"title": "t", "body": "b"}, "whatsapp": {"body": "b"}, "email": {"subject": "s", "body": "b"}}},
        "ack_label": "I have read this"}


def test_replayed_recipient_gets_no_cds_call_and_is_marked_simulated(tbl):
    ledger.put_recipient(tbl, "N1", {"recipient_id": "r-9", "rcs_capable": True, "phone": "+1", "live_device": False})
    item = ledger.get_recipient(tbl, "N1", "r-9")
    mid = dispatch.send_rung(tbl, META, item, "rcs", 1, clients={})  # empty clients: any real call would KeyError
    after = ledger.get_recipient(tbl, "N1", "r-9")
    assert mid == "replay-rcs-r-9" and after["state"] == "sent" and after["stage"] == 1
    assert after["tier_history"][0]["replayed"] is True
    assert ledger.lookup_message(tbl, mid)["recipient_id"] == "r-9"


def test_live_recipient_records_provider_message_id(tbl):
    class FakeRcs:
        def send_rcs_message(self, **kw):
            assert kw["MessageFeedbackEnabled"] is True
            return {"MessageId": "real-1"}

    ledger.put_recipient(tbl, "N1", {"recipient_id": "r-1", "rcs_capable": True, "phone": "+1555", "live_device": True})
    item = ledger.get_recipient(tbl, "N1", "r-1")
    assert dispatch.send_rung(tbl, META, item, "rcs", 1, clients={"rcs": FakeRcs()}) == "real-1"
    after = ledger.get_recipient(tbl, "N1", "r-1")
    assert after["tier_history"][0]["replayed"] is False and after["tier_history"][0]["provider_message_id"] == "real-1"
