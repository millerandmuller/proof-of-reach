"""SQS-wrapped delivery: unwrap, partial-batch failures, and the all-fail raise."""
import json

import pytest

from common import ledger
from common.tiers import build_postback
from consumers import batch, rcs_events, rcs_twoway

NOTICE, RID = "HX200-2026-10", "r-1"


def sqs_record(message: dict, mid="q-1", ts="2026-10-26T12:00:50.000Z"):
    body = {"Type": "Notification", "MessageId": "sns-1", "Timestamp": ts, "Message": json.dumps(message)}
    return {"messageId": mid, "body": json.dumps(body)}


def seed(tbl):
    ledger.put_recipient(tbl, NOTICE, {"recipient_id": RID, "rcs_capable": True, "phone": "+1", "live_device": False})
    ledger.register_message(tbl, "replay-rcs-r-1", NOTICE, RID, "rcs")
    ledger.record_event(tbl, {"notice_id": NOTICE, "recipient_id": RID, "channel": "rcs", "event": "SENT", "tier": "sent",
                              "provider_message_id": "replay-rcs-r-1", "provider_timestamp": "t0", "raw": {"x": 1}})


def test_sqs_wrapped_status_event_is_processed(tbl):
    seed(tbl)
    msg = {"eventType": "RCS_READ", "eventTimestamp": 1781661268000, "messageId": "replay-rcs-r-1", "messageStatus": "READ"}
    out = rcs_events.handler({"Records": [sqs_record(msg)]})
    assert out == {"processed": 1, "batchItemFailures": []}
    assert ledger.get_recipient(tbl, NOTICE, RID)["state"] == "read"


def test_sqs_wrapped_postback_uses_the_sns_timestamp(tbl):
    seed(tbl)
    body = json.dumps({"type": "SUGGESTION", "text": "ok", "postbackData": build_postback(NOTICE, RID)})
    rcs_twoway.handler({"Records": [sqs_record({"messageBody": body, "inboundMessageId": "replay-in-1",
                                                "previousPublishedMessageId": "replay-rcs-r-1"}, ts="2026-10-26T12:01:00.000Z")]})
    item = ledger.get_recipient(tbl, NOTICE, RID)
    assert item["state"] == "acknowledged" and item["tier_history"][-1]["at"] == "2026-10-26T12:01:00.000Z"


def test_one_bad_record_is_reported_alone_the_rest_are_applied(tbl):
    seed(tbl)
    good = {"eventType": "RCS_READ", "eventTimestamp": 1781661268000, "messageId": "replay-rcs-r-1", "messageStatus": "READ"}
    bad = {"eventType": "RCS_DELIVERED", "messageStatus": "DELIVERED", "messageId": "replay-rcs-r-1", "eventTimestamp": "not-a-number"}
    out = rcs_events.handler({"Records": [sqs_record(good, "q-good"), sqs_record(bad, "q-bad")]})
    assert out["batchItemFailures"] == [{"itemIdentifier": "q-bad"}] and out["processed"] == 1
    assert ledger.get_recipient(tbl, NOTICE, RID)["state"] == "read"


def test_batch_where_every_record_fails_raises(tbl):
    bad = {"eventType": "RCS_DELIVERED", "messageStatus": "DELIVERED", "messageId": "m", "eventTimestamp": "nope"}
    with pytest.raises(RuntimeError, match="all 2 records"):
        rcs_events.handler({"Records": [sqs_record(bad, "a"), sqs_record(bad, "b")]})


def test_unwrap_accepts_both_shapes():
    m = {"a": 1}
    assert batch.unwrap(sqs_record(m, "q9"))[0] == m
    assert batch.unwrap({"Sns": {"Message": json.dumps(m), "Timestamp": "t", "MessageId": "s"}}) == (m, "t", "s")
