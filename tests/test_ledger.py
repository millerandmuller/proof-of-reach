import pytest

from common import ledger
from common.tiers import build_postback, parse_postback

NOTICE = "HX200-2026-10"


def recipient(rid="r-1", **kw):
    base = {"recipient_id": rid, "segment": "consumer", "phone": "+15550001", "email": "a@example.test",
            "rcs_capable": True, "whatsapp": True, "live_device": False}
    base.update(kw)
    return base


def env(tier, event, mid="m1", ts="2026-10-26T12:00:00Z", channel="rcs", rid="r-1", **kw):
    e = {"notice_id": NOTICE, "recipient_id": rid, "channel": channel, "event": event, "tier": tier,
         "provider_message_id": mid, "provider_timestamp": ts, "raw": {"messageId": mid, "eventType": event}}
    e.update(kw)
    return e


def test_postback_roundtrip_and_embedded():
    pb = build_postback(NOTICE, "r-000412")
    assert pb == "por:v1:HX200-2026-10:r-000412:ack"
    assert parse_postback(pb) == (NOTICE, "r-000412")
    assert parse_postback(f"https://x.example/ack.html?p={pb}") == (NOTICE, "r-000412")
    assert parse_postback("garbage") is None
    assert parse_postback("por:v1:only:three") is None


def test_tier_progression_and_by_channel(tbl):
    ledger.put_recipient(tbl, NOTICE, recipient())
    for tier, ev in (("sent", "SENT"), ("delivered", "RCS_DELIVERED"), ("read", "RCS_READ"), ("acknowledged", "SUGGESTION_POSTBACK")):
        r = ledger.record_event(tbl, env(tier, ev, ts=f"2026-10-26T12:00:0{len(ev) % 9}Z"))
        assert r["status"] == "applied"
    item = ledger.get_recipient(tbl, NOTICE, "r-1")
    assert item["state"] == "acknowledged"
    assert item["by_channel"] == {"rcs": "acknowledged"}
    assert [h["state"] for h in item["tier_history"]] == ["sent", "delivered", "read", "acknowledged"]


def test_duplicate_event_changes_nothing(tbl):
    ledger.put_recipient(tbl, NOTICE, recipient())
    e = env("delivered", "RCS_DELIVERED")
    assert ledger.record_event(tbl, e)["status"] == "applied"
    before = ledger.get_recipient(tbl, NOTICE, "r-1")
    assert ledger.record_event(tbl, e)["status"] == "duplicate"
    after = ledger.get_recipient(tbl, NOTICE, "r-1")
    assert after["version"] == before["version"]
    assert len(after["tier_history"]) == 1


def test_tier_never_regresses(tbl):
    ledger.put_recipient(tbl, NOTICE, recipient())
    ledger.record_event(tbl, env("acknowledged", "SUGGESTION_POSTBACK", ts="t2"))
    r = ledger.record_event(tbl, env("delivered", "RCS_DELIVERED", ts="t3"))
    assert r["status"] == "no_change"
    item = ledger.get_recipient(tbl, NOTICE, "r-1")
    assert item["state"] == "acknowledged"
    assert item["tier_history"][-1]["counted"] is False


def test_second_tap_is_logged_not_counted(tbl):
    ledger.put_recipient(tbl, NOTICE, recipient())
    ledger.record_event(tbl, env("acknowledged", "SUGGESTION_POSTBACK", ts="t1"))
    ledger.record_event(tbl, env("acknowledged", "SUGGESTION_POSTBACK", ts="t2"))
    item = ledger.get_recipient(tbl, NOTICE, "r-1")
    assert [h["counted"] for h in item["tier_history"]] == [True, False]


def test_acknowledgment_without_event_id_is_refused(tbl):
    ledger.put_recipient(tbl, NOTICE, recipient())
    with pytest.raises(ValueError):
        ledger.record_event(tbl, env("acknowledged", "SUGGESTION_POSTBACK", mid=None))
    with pytest.raises(ValueError):
        ledger.record_event(tbl, env("acknowledged", "SUGGESTION_POSTBACK", raw=None))
    assert ledger.get_recipient(tbl, NOTICE, "r-1")["state"] == "none"


def test_bot_open_is_stored_not_counted(tbl):
    ledger.put_recipient(tbl, NOTICE, recipient())
    ledger.record_event(tbl, env("delivered", "Delivery", channel="email", mid="e1", ts="t1"))
    r = ledger.record_event(tbl, env("read", "Open", channel="email", mid="e1", ts="t2", is_bot=True))
    assert r["status"] == "no_change"
    item = ledger.get_recipient(tbl, NOTICE, "r-1")
    assert item["state"] == "delivered"
    assert item["tier_history"][-1]["is_bot"] is True


def test_unmatched_event_lands_in_unmatched_list(tbl):
    r = ledger.record_event(tbl, env("acknowledged", "SUGGESTION_POSTBACK", rid=None))
    assert r["status"] == "unmatched"
    rows = ledger.query_prefix(tbl, NOTICE, "unmatched#")
    assert len(rows) == 1


def test_unreachable_never_overrides_ack_and_proof_beats_verdict(tbl):
    ledger.put_recipient(tbl, NOTICE, recipient("r-1"))
    ledger.put_recipient(tbl, NOTICE, recipient("r-2"))
    ledger.record_event(tbl, env("acknowledged", "SUGGESTION_POSTBACK", rid="r-1"))
    assert ledger.mark_unreachable(tbl, NOTICE, "r-1", "x") is False
    assert ledger.get_recipient(tbl, NOTICE, "r-1")["state"] == "acknowledged"
    assert ledger.mark_unreachable(tbl, NOTICE, "r-2", "no ack") is True
    ledger.record_event(tbl, env("acknowledged", "SUGGESTION_POSTBACK", rid="r-2", mid="m2"))
    item = ledger.get_recipient(tbl, NOTICE, "r-2")
    assert item["state"] == "acknowledged" and item["unreachable_reason"] is None


def test_raw_event_stored_verbatim(tbl):
    ledger.put_recipient(tbl, NOTICE, recipient())
    ledger.record_event(tbl, env("read", "RCS_READ"))
    rows = ledger.query_prefix(tbl, NOTICE, "event#")
    assert '"eventType": "RCS_READ"' in rows[0]["raw_json"]


def test_failed_write_leaves_no_orphan_event_and_redelivery_applies_the_ack(tbl):
    """Regression: the event row used to be written before the recipient update, so a failure
    in between turned the redelivered acknowledgment into a 'duplicate' and lost it for good."""
    from botocore.exceptions import ClientError

    ledger.put_recipient(tbl, NOTICE, recipient())
    client = tbl.meta.client
    real = client.transact_write_items
    calls = {"n": 0}

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow down"}}, "TransactWriteItems")
        return real(**kw)

    client.transact_write_items = flaky
    try:
        ack = env("acknowledged", "SUGGESTION_POSTBACK", mid="in-1")
        with pytest.raises(ClientError):
            ledger.record_event(tbl, ack)
        assert ledger.get_recipient(tbl, NOTICE, "r-1")["state"] == "none"
        assert ledger.query_prefix(tbl, NOTICE, "event#") == []  # no orphan event row
        assert ledger.record_event(tbl, ack)["status"] == "applied"  # the redelivery is not a duplicate
    finally:
        client.transact_write_items = real
    assert ledger.get_recipient(tbl, NOTICE, "r-1")["state"] == "acknowledged"


def test_contention_exhaustion_raises_and_is_retriable(tbl, monkeypatch):
    ledger.put_recipient(tbl, NOTICE, recipient())
    monkeypatch.setattr(ledger, "_backoff", lambda attempt: None)
    from botocore.exceptions import ClientError
    client = tbl.meta.client
    real = client.transact_write_items

    def always_conflict(**kw):
        raise ClientError({"Error": {"Code": "TransactionCanceledException", "Message": "x"},
                           "CancellationReasons": [{"Code": "None"}, {"Code": "ConditionalCheckFailed"}]}, "TransactWriteItems")

    client.transact_write_items = always_conflict
    try:
        with pytest.raises(RuntimeError, match="contention"):
            ledger.record_event(tbl, env("acknowledged", "SUGGESTION_POSTBACK", mid="in-2"))
    finally:
        client.transact_write_items = real
    assert ledger.record_event(tbl, env("acknowledged", "SUGGESTION_POSTBACK", mid="in-2"))["status"] == "applied"


def test_postback_survives_percent_encoding_by_mail_clients():
    from urllib.parse import quote
    link = "https://d.example/ack.html?p=" + quote(build_postback(NOTICE, "r-9"), safe="")
    assert "%3A" in link and parse_postback(link) == (NOTICE, "r-9")


def test_a_real_open_is_remembered_after_a_later_click_but_a_bot_open_is_not(tbl):
    ledger.put_recipient(tbl, NOTICE, recipient("opened"))
    ledger.put_recipient(tbl, NOTICE, recipient("botted"))
    for rid in ("opened", "botted"):
        ledger.record_event(tbl, env("delivered", "Delivery", channel="email", rid=rid, mid=f"d-{rid}"))
    ledger.record_event(tbl, env("read", "Open", channel="email", rid="opened", mid="o1"))
    ledger.record_event(tbl, env("read", "Open", channel="email", rid="botted", mid="o2", is_bot=True))
    for rid in ("opened", "botted"):
        ledger.record_event(tbl, env("acknowledged", "Click", channel="email", rid=rid, mid=f"c-{rid}"))
    assert ledger.get_recipient(tbl, NOTICE, "opened")["read_seen"] == ["email"]
    assert not ledger.get_recipient(tbl, NOTICE, "botted").get("read_seen")
