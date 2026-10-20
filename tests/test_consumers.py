"""Consumers exercised with the payload shapes from the AWS docs (S-03, S-04, S-16, S-23)."""
import json
from datetime import datetime, timezone

import pytest

from common import feedback, ledger
from common.tiers import build_postback
from consumers import rcs_events, rcs_twoway, ses_events, whatsapp_events

NOTICE = "HX200-2026-10"
RID = "r-000412"
PB = build_postback(NOTICE, RID)


def sns(message: dict, ts="2026-10-26T12:00:50.000Z", mid="sns-1"):
    return {"Records": [{"Sns": {"Message": json.dumps(message), "Timestamp": ts, "MessageId": mid}}]}


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def row(tbl):
    ledger.put_recipient(tbl, NOTICE, {"recipient_id": RID, "phone": "+15550001", "email": "a@example.test",
                                        "rcs_capable": True, "whatsapp": True, "live_device": True})
    return tbl


def send(tbl, channel, mid, at=None):
    ledger.register_message(tbl, mid, NOTICE, RID, channel)
    ledger.record_event(tbl, {"notice_id": NOTICE, "recipient_id": RID, "channel": channel, "event": "SENT", "tier": "sent",
                              "provider_message_id": mid, "provider_timestamp": at or now_iso(), "raw": {"MessageId": mid}})


RCS_DELIVERED = {"eventType": "RCS_DELIVERED", "eventTimestamp": 1781661267660, "messageId": "rcs-1",
                 "messageStatus": "DELIVERED", "rcsBusinessId": "agent", "agentIsoCountryCode": "US"}
RCS_READ = {"eventType": "RCS_READ", "eventTimestamp": 1781661268000, "messageId": "rcs-1", "messageStatus": "READ"}


def test_rcs_read_moves_row_and_stores_raw(row):
    send(row, "rcs", "rcs-1")
    rcs_events.handler(sns(RCS_DELIVERED))
    rcs_events.handler(sns(RCS_READ))
    item = ledger.get_recipient(row, NOTICE, RID)
    assert item["state"] == "read" and item["by_channel"] == {"rcs": "read"}
    raws = [r["raw_json"] for r in ledger.query_prefix(row, NOTICE, "event#")]
    assert any('"RCS_READ"' in r for r in raws)


def test_rcs_duplicate_sns_delivery_is_idempotent(row):
    send(row, "rcs", "rcs-1")
    rcs_events.handler(sns(RCS_READ))
    v = ledger.get_recipient(row, NOTICE, RID)["version"]
    rcs_events.handler(sns(RCS_READ))
    assert ledger.get_recipient(row, NOTICE, RID)["version"] == v


def test_rcs_undeliverable_records_failure_without_changing_state(row):
    send(row, "rcs", "rcs-1")
    rcs_events.handler(sns({**RCS_DELIVERED, "messageStatus": "UNDELIVERABLE", "messageStatusDescription": "Device not RCS capable"}))
    item = ledger.get_recipient(row, NOTICE, RID)
    assert item["state"] == "sent"
    assert item["last_failure"]["reason"] == "Device not RCS capable"


def test_rcs_fallback_to_sms_stays_sent(row):
    send(row, "rcs", "rcs-1")
    rcs_events.handler(sns({"eventType": "RCS_FALLEN_BACK_TO_SMS", "eventTimestamp": 1781661268000, "messageId": "rcs-1"}))
    assert ledger.get_recipient(row, NOTICE, RID)["state"] == "sent"


def postback_message(data=PB):
    return {"originationNumber": "12679164820", "destinationNumber": "rcs-agent",
            "messageBody": json.dumps({"type": "SUGGESTION", "text": "Switched off", "postbackData": data}),
            "inboundMessageId": "in-1"}


def test_rcs_tap_acknowledges_and_closes_loop(row, monkeypatch):
    calls = []

    class FakeClient:
        def put_message_feedback(self, **kw):
            calls.append(kw)

    monkeypatch.setattr(feedback.boto3, "client", lambda *a, **k: FakeClient())
    send(row, "rcs", "rcs-1")
    rcs_twoway.handler(sns(postback_message()))
    item = ledger.get_recipient(row, NOTICE, RID)
    assert item["state"] == "acknowledged" and item["feedback_sent"] is True
    assert calls == [{"MessageId": "rcs-1", "MessageFeedbackStatus": "RECEIVED"}]
    # second tap: logged, not counted, no second feedback call
    rcs_twoway.handler(sns(postback_message(), ts="2026-10-26T12:01:50.000Z", mid="sns-2"))
    assert len(calls) == 1
    assert [h["counted"] for h in ledger.get_recipient(row, NOTICE, RID)["tier_history"] if h["state"] == "acknowledged"] == [True, False]


def test_feedback_skipped_outside_one_hour_window(row, monkeypatch):
    monkeypatch.setattr(feedback.boto3, "client", lambda *a, **k: pytest.fail("must not call AWS"))
    send(row, "rcs", "rcs-1", at="2020-01-01T00:00:00Z")
    rcs_twoway.handler(sns(postback_message()))
    item = ledger.get_recipient(row, NOTICE, RID)
    assert item["state"] == "acknowledged" and item["feedback_sent"] is False


def test_postback_without_correlation_lands_in_unmatched(tbl):
    rcs_twoway.handler(sns(postback_message("something-else")))
    # no notice id on the envelope: stored under the unknown notice
    assert len(ledger.query_prefix(tbl, "unknown", "unmatched#")) == 1


def test_rcs_typing_indicator_ignored(row):
    out = rcs_twoway.handler(sns({"messageBody": json.dumps({"type": "RCS_TYPING"}), "inboundMessageId": "x"}))
    assert out["processed"] == 0


def test_free_text_reply_is_stored_for_summarizer(row):
    send(row, "rcs", "rcs-1")
    rcs_twoway.handler(sns({"originationNumber": "1", "messageBody": "where is my refund?", "inboundMessageId": "in-9",
                            "previousPublishedMessageId": "rcs-1"}))
    replies = ledger.query_prefix(row, NOTICE, "reply#")
    assert [r["text"] for r in replies] == ["where is my refund?"]
    assert ledger.get_recipient(row, NOTICE, RID)["state"] == "sent"


def wa(entry_value):
    return {"context": {"MetaWabaIds": []}, "aws_account_id": "1", "message_timestamp": "2026-10-26T12:00:00Z",
            "messageId": "aws-1",
            "whatsAppWebhookEntry": json.dumps({"id": "1", "changes": [{"field": "messages", "value": entry_value}]})}


def test_whatsapp_delivered_read_and_button_reply(row):
    send(row, "whatsapp", "wamid.A")
    whatsapp_events.handler(sns(wa({"statuses": [{"id": "wamid.A", "status": "delivered", "timestamp": "1736379042", "recipient_id": "1"}]})))
    whatsapp_events.handler(sns(wa({"statuses": [{"id": "wamid.A", "status": "read", "timestamp": "1736379050", "recipient_id": "1"}]})))
    assert ledger.get_recipient(row, NOTICE, RID)["state"] == "read"
    reply = {"messages": [{"from": "1", "id": "wamid.B", "timestamp": "1736379060", "type": "interactive",
                           "context": {"id": "wamid.A"},
                           "interactive": {"type": "button_reply", "button_reply": {"id": PB, "title": "I have read this"}}}]}
    whatsapp_events.handler(sns(wa(reply)))
    item = ledger.get_recipient(row, NOTICE, RID)
    assert item["state"] == "acknowledged"
    assert item["by_channel"]["whatsapp"] == "acknowledged"


def test_whatsapp_read_receipts_off_stays_delivered(row):
    send(row, "whatsapp", "wamid.A")
    whatsapp_events.handler(sns(wa({"statuses": [{"id": "wamid.A", "status": "delivered", "timestamp": "1736379042"}]})))
    assert ledger.get_recipient(row, NOTICE, RID)["state"] == "delivered"


def test_whatsapp_failed_status_records_reason(row):
    send(row, "whatsapp", "wamid.A")
    whatsapp_events.handler(sns(wa({"statuses": [{"id": "wamid.A", "status": "failed", "timestamp": "1736379042",
                                                  "errors": [{"code": 131047, "title": "Re-engagement message"}]}]})))
    item = ledger.get_recipient(row, NOTICE, RID)
    assert item["state"] == "sent" and item["last_failure"]["reason"] == "Re-engagement message"


def ses(event_type, **extra):
    return {"eventType": event_type, "mail": {"messageId": "ses-1", "timestamp": "2026-10-26T12:00:00.000Z"}, **extra}


def test_ses_delivery_open_click(row):
    send(row, "email", "ses-1")
    ses_events.handler(sns(ses("Delivery", delivery={"timestamp": "2026-10-26T12:00:02.000Z"})))
    ses_events.handler(sns(ses("Open", open={"timestamp": "2026-10-26T12:00:10.000Z", "isBotEvent": "Unlikely"})))
    assert ledger.get_recipient(row, NOTICE, RID)["state"] == "read"
    link = f"https://d111.cloudfront.net/ack.html?p={PB}"
    ses_events.handler(sns(ses("Click", click={"timestamp": "2026-10-26T12:00:20.000Z", "link": link, "isBotEvent": "Unlikely"})))
    assert ledger.get_recipient(row, NOTICE, RID)["state"] == "acknowledged"


def test_ses_bot_open_and_bot_click_do_not_count(row):
    send(row, "email", "ses-1")
    ses_events.handler(sns(ses("Delivery", delivery={"timestamp": "2026-10-26T12:00:02.000Z"})))
    ses_events.handler(sns(ses("Open", open={"timestamp": "2026-10-26T12:00:10.000Z", "isBotEvent": "Likely"})))
    link = f"https://x.example/ack.html?p={PB}"
    ses_events.handler(sns(ses("Click", click={"timestamp": "2026-10-26T12:00:11.000Z", "link": link, "isBotEvent": "Likely"})))
    item = ledger.get_recipient(row, NOTICE, RID)
    assert item["state"] == "delivered"
    assert [h["is_bot"] for h in item["tier_history"][-2:]] == [True, True]


def test_ses_click_on_other_link_is_ignored_and_bounce_records_reason(row):
    send(row, "email", "ses-1")
    out = ses_events.handler(sns(ses("Click", click={"timestamp": "t", "link": "https://example.com/other", "isBotEvent": "Unlikely"})))
    assert out["processed"] == 0
    ses_events.handler(sns(ses("Bounce", bounce={"bounceType": "Permanent", "bounceSubType": "NoEmail", "timestamp": "2026-10-26T12:00:05.000Z"})))
    assert ledger.get_recipient(row, NOTICE, RID)["last_failure"]["reason"] == "bounce Permanent/NoEmail"


def _iso_ago(minutes):
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.mark.parametrize("minutes_ago,expect_call", [(30, True), (59, True), (61, False), (120, False)])
def test_feedback_window_boundary_is_one_hour(row, monkeypatch, minutes_ago, expect_call):
    calls = []

    class FakeClient:
        def put_message_feedback(self, **kw):
            calls.append(kw)

    monkeypatch.setattr(feedback.boto3, "client", lambda *a, **k: FakeClient())
    send(row, "rcs", "rcs-1", at=_iso_ago(minutes_ago))
    rcs_twoway.handler(sns(postback_message()))
    assert bool(calls) is expect_call


def test_whatsapp_acknowledgment_does_not_close_the_rcs_feedback_loop(row, monkeypatch):
    monkeypatch.setattr(feedback.boto3, "client", lambda *a, **k: pytest.fail("RCS feedback must not fire for a WhatsApp ack"))
    send(row, "rcs", "rcs-1")
    send(row, "whatsapp", "wamid.A")
    reply = {"messages": [{"from": "1", "id": "wamid.B", "timestamp": "1736379060", "type": "interactive", "context": {"id": "wamid.A"},
                           "interactive": {"type": "button_reply", "button_reply": {"id": PB, "title": "ok"}}}]}
    whatsapp_events.handler(sns(wa(reply)))
    item = ledger.get_recipient(row, NOTICE, RID)
    assert item["state"] == "acknowledged" and item["feedback_sent"] is False
