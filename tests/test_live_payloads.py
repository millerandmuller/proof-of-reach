"""Payload shapes captured from the first run on real devices: WhatsApp statuses and free text, and the RCS OpenUrl tap.

All identifiers are synthetic stand-ins with the real shape. The WhatsApp entry travels as a JSON string inside the SNS message.
"""
import copy
import json

import pytest

from common import ledger, normalize
from common.tiers import build_postback, build_url_postback
from consumers import batch, rcs_twoway, whatsapp_events
from senders import dispatch, whatsapp

NOTICE = "HX200-2026-10"
RID = "live-phone-b"
PHONE = "+15550100222"
AWS_SEND_ID = "00000000-0000-4000-8000-000000000001"
WAMID = "wamid.TESTSTATUS0001"
PB = build_postback(NOTICE, RID)

FAILED_ENTRY = {
    "id": "200000000000001",
    "changes": [{
        "value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "15550100999", "phone_number_id": "100000000000001"},
            "contacts": [{"wa_id": "15550100222", "user_id": "US.100000000000002"}],
            "statuses": [{
                "id": WAMID, "status": "failed", "timestamp": "1792624378",
                "recipient_id": "15550100222", "recipient_user_id": "US.100000000000002",
                "errors": [{"code": 131031, "title": "Business Account locked", "message": "Business Account locked",
                            "error_data": {"details": "Business account has been locked."},
                            "href": "https://developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes/"}],
            }],
        },
        "field": "messages",
    }],
}
FAILED = {
    "aws_account_id": "123456789012",
    "context": {"MetaPhoneNumberIds": [{"metaPhoneNumberId": "100000000000001"}], "MetaWabaIds": [{"wabaId": "200000000000001"}]},
    "messageId": AWS_SEND_ID,
    "message_timestamp": "2026-10-21T23:12:59.029121914Z",
    "whatsAppWebhookEntry": json.dumps(FAILED_ENTRY),
}
INBOUND_ENTRY = {
    "id": "200000000000001",
    "changes": [{
        "value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "15550100999", "phone_number_id": "100000000000001"},
            "contacts": [{"profile": {"name": "Test Phone B"}, "wa_id": "15550100222", "user_id": "US.100000000000002"}],
            "messages": [{"from": "15550100222", "from_user_id": "US.100000000000002",
                          "id": "wamid.TESTINBOUND0001",
                          "timestamp": "1792624153", "text": {"body": "hi"}, "type": "text"}],
        },
        "field": "messages",
    }],
}
INBOUND_TEXT = {
    "aws_account_id": "123456789012",
    "context": {"MetaPhoneNumberIds": [{"metaPhoneNumberId": "100000000000001"}], "MetaWabaIds": [{"wabaId": "200000000000001"}]},
    "message_timestamp": "2026-10-21T23:09:16.789659589Z",
    "whatsAppWebhookEntry": json.dumps(INBOUND_ENTRY),
}
RCS_URL_TAP = {
    "destinationNumber": "rcs-00000000000000000000000000000001",
    "inboundMessageId": "TESTINBOUNDRCS0001",
    "messageBody": json.dumps({"type": "SUGGESTION", "text": "Read the notice", "postbackData": "por:v1:HX200-2026-10:live-phone-a:ack:url"}),
    "originationNumber": "+15550100111",
    "rcsBusinessId": "test_agent",
}


def status_message(status, opaque=None, aws_id=AWS_SEND_ID, wamid=WAMID):
    """The real failed event reshaped into another status of the same message."""
    entry = copy.deepcopy(FAILED_ENTRY)
    st = entry["changes"][0]["value"]["statuses"][0]
    st.update(status=status, id=wamid)
    st.pop("errors", None)
    if opaque:
        st["biz_opaque_callback_data"] = opaque
    msg = {**FAILED, "whatsAppWebhookEntry": json.dumps(entry)}
    if aws_id:
        msg["messageId"] = aws_id
    else:
        msg.pop("messageId")
    return msg


def sns(message, ts="2026-10-21T23:13:00.000Z"):
    return {"Records": [{"Sns": {"Message": json.dumps(message), "Timestamp": ts, "MessageId": "sns-1"}}]}


@pytest.fixture
def phone_b(tbl):
    ledger.put_recipient(tbl, NOTICE, {"recipient_id": RID, "phone": PHONE, "email": "b@example.test",
                                        "rcs_capable": True, "whatsapp": True, "live_device": True})
    return tbl


def sent(tbl, with_phone=True):
    ledger.register_message(tbl, AWS_SEND_ID, NOTICE, RID, "whatsapp")
    if with_phone:
        ledger.register_phone(tbl, PHONE, NOTICE, RID, "2026-10-21T23:12:00Z")
    ledger.record_event(tbl, {"notice_id": NOTICE, "recipient_id": RID, "channel": "whatsapp", "event": "SENT", "tier": "sent",
                              "provider_message_id": AWS_SEND_ID, "provider_timestamp": "2026-10-21T23:12:00Z",
                              "raw": {"MessageId": AWS_SEND_ID}})


# ------------------------------------------------------------------ P1 correlation

def test_failed_status_reaches_the_recipient_by_the_aws_send_id(phone_b):
    sent(phone_b, with_phone=False)
    whatsapp_events.handler(sns(FAILED))
    item = ledger.get_recipient(phone_b, NOTICE, RID)
    assert item["last_failure"]["reason"] == "Business Account locked"
    assert item["state"] == "sent"  # a failure never moves the tier
    assert ledger.query_prefix(phone_b, "unknown", "unmatched#") == []


def test_failed_status_reaches_the_recipient_by_the_echoed_postback(phone_b):
    sent(phone_b, with_phone=False)
    ledger.register_message(phone_b, AWS_SEND_ID, "other", "other", "whatsapp")  # the id map would point elsewhere
    msg = status_message("failed", opaque=PB)
    entry = json.loads(msg["whatsAppWebhookEntry"])
    entry["changes"][0]["value"]["statuses"][0]["errors"] = FAILED_ENTRY["changes"][0]["value"]["statuses"][0]["errors"]
    msg["whatsAppWebhookEntry"] = json.dumps(entry)
    whatsapp_events.handler(sns(msg))
    assert ledger.get_recipient(phone_b, NOTICE, RID)["last_failure"]["reason"] == "Business Account locked"


def test_failed_status_reaches_the_recipient_by_phone_alone(phone_b):
    sent(phone_b)
    msg = {k: v for k, v in FAILED.items() if k != "messageId"}  # no AWS id, no echoed postback
    whatsapp_events.handler(sns(msg))
    assert ledger.get_recipient(phone_b, NOTICE, RID)["last_failure"]["reason"] == "Business Account locked"


def test_delivered_and_read_statuses_move_the_row(phone_b):
    sent(phone_b)
    whatsapp_events.handler(sns(status_message("delivered")))
    assert ledger.get_recipient(phone_b, NOTICE, RID)["state"] == "delivered"
    whatsapp_events.handler(sns(status_message("read")))
    item = ledger.get_recipient(phone_b, NOTICE, RID)
    assert item["state"] == "read" and item["by_channel"]["whatsapp"] == "read"


def test_first_status_teaches_the_map_the_wamid(phone_b):
    sent(phone_b)
    assert ledger.lookup_message(phone_b, WAMID) is None
    whatsapp_events.handler(sns(status_message("delivered")))
    assert ledger.lookup_message(phone_b, WAMID)["recipient_id"] == RID
    # a later status carrying nothing but the wamid still finds the row
    phone_b.delete_item(Key={"pk": f"phone#{PHONE}", "sk": "map"})
    whatsapp_events.handler(sns(status_message("read", aws_id=None)))
    assert ledger.get_recipient(phone_b, NOTICE, RID)["state"] == "read"


def test_free_text_reply_is_stored_on_the_recipient_by_phone(phone_b):
    sent(phone_b)
    whatsapp_events.handler(sns(INBOUND_TEXT))
    replies = ledger.query_prefix(phone_b, NOTICE, "reply#")
    assert [(r["recipient_id"], r["text"], r["channel"]) for r in replies] == [(RID, "hi", "whatsapp")]


def test_unknown_phone_stays_unmatched_and_visible(phone_b):
    whatsapp_events.handler(sns(INBOUND_TEXT))  # nothing was ever sent to this number
    assert ledger.query_prefix(phone_b, NOTICE, "reply#") == []
    assert len(ledger.query_prefix(phone_b, "unknown", "unmatched#")) == 1


def test_send_sets_the_callback_data_and_records_the_phone(tbl, monkeypatch):
    monkeypatch.setattr("common.config.WHATSAPP_TEMPLATE_NAME", "")
    assert whatsapp.build_message(PHONE, {"body": "b", "ack_label": "ok"}, PB)["biz_opaque_callback_data"] == PB
    monkeypatch.setattr("common.config.WHATSAPP_TEMPLATE_NAME", "recall_notice")
    assert whatsapp.build_message(PHONE, {"body": "b", "ack_label": "ok"}, PB)["biz_opaque_callback_data"] == PB

    class Client:
        def send_whatsapp_message(self, **kw):
            return {"messageId": AWS_SEND_ID}
    ledger.put_recipient(tbl, NOTICE, {"recipient_id": RID, "phone": PHONE, "whatsapp": True, "live_device": True})
    meta = {"texts": {"consumer": {"whatsapp": {"body": "b"}}}}
    dispatch.send_rung(tbl, meta, ledger.get_recipient(tbl, NOTICE, RID), "whatsapp", 2, {"whatsapp": Client()})
    assert ledger.lookup_phone(tbl, PHONE)["recipient_id"] == RID
    assert ledger.lookup_message(tbl, AWS_SEND_ID)["recipient_id"] == RID


def test_normalizer_derives_phone_from_the_digits_meta_sends():
    env = normalize.normalize_whatsapp(FAILED)[0]
    assert env["lookup_phone"] == PHONE and env["alt_lookup_ids"] == [AWS_SEND_ID] and env["failure_reason"] == "Business Account locked"
    assert normalize.normalize_whatsapp(INBOUND_TEXT)[0]["lookup_phone"] == PHONE


# ------------------------------------------------------------------ P2 OpenUrl tap

def test_openurl_tap_is_a_read_not_an_acknowledgment(tbl):
    ledger.put_recipient(tbl, NOTICE, {"recipient_id": "live-phone-a", "phone": "+15550100333", "rcs_capable": True, "live_device": True})
    env = normalize.normalize_rcs_inbound(RCS_URL_TAP, "2026-10-21T23:10:38.000Z")
    assert (env["event"], env["tier"], env["notice_id"], env["recipient_id"]) == ("SUGGESTION_URL", "read", NOTICE, "live-phone-a")
    rcs_twoway.handler(sns(RCS_URL_TAP))
    item = ledger.get_recipient(tbl, NOTICE, "live-phone-a")
    assert item["state"] == "read" and item["feedback_sent"] is False
    assert ledger.query_prefix(tbl, "unknown", "unmatched#") == []


def test_ack_tap_after_the_openurl_tap_still_acknowledges(tbl):
    ledger.put_recipient(tbl, NOTICE, {"recipient_id": "live-phone-a", "phone": "+15550100333", "rcs_capable": True, "live_device": True})
    rcs_twoway.handler(sns(RCS_URL_TAP))
    ack = copy.deepcopy(RCS_URL_TAP)
    ack["inboundMessageId"] = "in-2"
    ack["messageBody"] = json.dumps({"type": "SUGGESTION", "text": "ok", "postbackData": build_postback(NOTICE, "live-phone-a")})
    env = normalize.normalize_rcs_inbound(ack, "2026-10-21T23:11:00.000Z")
    assert env["tier"] == "acknowledged" and env["event"] == "SUGGESTION_POSTBACK"


def test_url_postback_is_never_parsed_as_an_acknowledgment():
    from common.tiers import parse_postback
    assert parse_postback(build_url_postback(PB)) is None


# ------------------------------------------------------------------ P3 / P4

def test_dump_script_decodes_the_whatsapp_entry():
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
    from dump_events import decode_raw
    decoded = decode_raw(json.dumps(FAILED))
    assert decoded["whatsAppWebhookEntry"]["changes"][0]["value"]["statuses"][0]["status"] == "failed"


def test_batch_logs_the_keys_of_a_record_that_produced_no_events(tbl, caplog):
    handler = batch.make_handler(lambda t, m, ts: 0)
    with caplog.at_level("INFO", logger="consumers.batch"):
        handler(sns({"eventType": "RCS_PENDING", "messageId": "x"}))
    assert any("eventType" in r.getMessage() and "messageId" in r.getMessage() for r in caplog.records)


# ------------------------------------------------------------------ hardening after review

def _button_message(kind, ident, context_id):
    entry = copy.deepcopy(INBOUND_ENTRY)
    msg = {"from": "15550100222", "id": "wamid.TESTBUTTON0001", "timestamp": "1792624200", "context": {"id": context_id}}
    if kind == "button":
        msg.update(type="button", button={"payload": ident, "text": "Stop"})
    else:
        msg.update(type="interactive", interactive={"type": "button_reply", "button_reply": {"id": ident, "title": "x"}})
    entry["changes"][0]["value"]["messages"] = [msg]
    return {**INBOUND_TEXT, "whatsAppWebhookEntry": json.dumps(entry)}


@pytest.mark.parametrize("kind", ["button", "button_reply"])
def test_a_button_we_did_not_issue_is_never_an_acknowledgment(phone_b, kind):
    sent(phone_b)
    whatsapp_events.handler(sns(status_message("delivered")))  # teaches the wamid, so context.id would resolve
    whatsapp_events.handler(sns(_button_message(kind, "STOP_PROMOTIONS", WAMID)))
    item = ledger.get_recipient(phone_b, NOTICE, RID)
    assert item["state"] == "delivered" and "acknowledged" not in item["by_channel"].values()
    assert len(ledger.query_prefix(phone_b, "unknown", "unmatched#")) == 1  # kept for inspection


@pytest.mark.parametrize("postback", [PB + ":URL", PB + ":url:url", PB.replace(":", "%3A") + "%3Aurl ", "garbage", ""])
def test_a_mangled_rcs_suggestion_is_never_an_acknowledgment(tbl, postback):
    ledger.put_recipient(tbl, NOTICE, {"recipient_id": RID, "phone": PHONE, "rcs_capable": True, "live_device": True})
    ledger.register_message(tbl, "rcs-1", NOTICE, RID, "rcs")
    msg = {"inboundMessageId": "in-x", "previousPublishedMessageId": "rcs-1",
           "messageBody": json.dumps({"type": "SUGGESTION", "text": "t", "postbackData": postback})}
    rcs_twoway.handler(sns(msg))
    assert ledger.get_recipient(tbl, NOTICE, RID)["state"] in ("none", "read")


def test_failure_reason_never_carries_the_providers_free_text(phone_b):
    sent(phone_b)
    entry = copy.deepcopy(FAILED_ENTRY)
    entry["changes"][0]["value"]["statuses"][0]["errors"] = [{"code": 131000, "message": "could not reach +15550100222"}]
    whatsapp_events.handler(sns({**FAILED, "whatsAppWebhookEntry": json.dumps(entry)}))
    assert ledger.get_recipient(phone_b, NOTICE, RID)["last_failure"]["reason"] == "error 131000"


def test_phone_map_matches_numbers_written_with_spaces(tbl):
    ledger.register_phone(tbl, "+1 555 010 0222", NOTICE, RID, "2026-10-21T23:12:00Z")
    assert ledger.lookup_phone(tbl, "+15550100222")["recipient_id"] == RID
    assert ledger.lookup_phone(tbl, "") is None and ledger.lookup_phone(tbl, "+") is None


def test_first_writer_wins_for_a_learned_message_id(tbl):
    ledger.register_message(tbl, "wamid.X", NOTICE, "first", "whatsapp", keep_existing=True)
    ledger.register_message(tbl, "wamid.X", NOTICE, "second", "whatsapp", keep_existing=True)
    assert ledger.lookup_message(tbl, "wamid.X")["recipient_id"] == "first"


def test_a_late_sent_status_does_not_clear_the_unreachable_verdict(phone_b):
    sent(phone_b)
    assert ledger.mark_unreachable(phone_b, NOTICE, RID, "never delivered") is True
    whatsapp_events.handler(sns(status_message("sent")))
    assert ledger.get_recipient(phone_b, NOTICE, RID)["state"] == "unreachable"
    whatsapp_events.handler(sns(status_message("delivered")))  # a real delivery still beats the verdict
    assert ledger.get_recipient(phone_b, NOTICE, RID)["state"] == "delivered"
