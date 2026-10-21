import pytest
from botocore.exceptions import ClientError

from common import ledger
from engine import ladder, rule_engine

N = "N1"
TEXTS = {"rcs": {"title": "t", "body": "b"}, "whatsapp": {"body": "b"}, "email": {"subject": "s", "body": "b"}}
META = {"texts": {"consumer": TEXTS}, "ack_label": "I read this", "window_seconds": 90}


def rec(rid, rcs=True, wa=True, email=True, live=False):
    return {"recipient_id": rid, "rcs_capable": rcs, "whatsapp": wa, "email": "a@x.test" if email else "",
            "phone": "+1555", "live_device": live}


def ack(tbl, rid):
    ledger.record_event(tbl, {"notice_id": N, "recipient_id": rid, "channel": "rcs", "event": "SUGGESTION_POSTBACK",
                              "tier": "acknowledged", "provider_message_id": f"in-{rid}", "provider_timestamp": "t", "raw": {"x": 1}})


@pytest.fixture
def notice(tbl):
    ledger.put_meta(tbl, N, dict(META))
    for r in (rec("a"), rec("b"), rec("c", rcs=False), rec("d", rcs=False, wa=False), rec("e", rcs=False, wa=False, email=False)):
        ledger.put_recipient(tbl, N, r)
    return tbl


def test_rung_for_is_per_recipient_ladder(notice):
    assert ladder.rung_for(ledger.get_recipient(notice, N, "a"), 1) == "rcs"
    assert ladder.rung_for(ledger.get_recipient(notice, N, "a"), 3) == "email"
    assert ladder.rung_for(ledger.get_recipient(notice, N, "c"), 1) == "whatsapp"
    assert ladder.rung_for(ledger.get_recipient(notice, N, "d"), 2) is None


def test_full_ladder_stops_after_stage_3_and_never_escalates_acknowledged(notice):
    scheduled = []
    sched = lambda nid, st, w: scheduled.append((nid, st, w))
    rule_engine.run_stage(notice, N, 1, clients={}, schedule=sched)
    ack(notice, "a")  # a acknowledges inside window 1
    rule_engine.run_stage(notice, N, 2, clients={}, schedule=sched)
    rule_engine.run_stage(notice, N, 3, clients={}, schedule=sched)
    rule_engine.run_stage(notice, N, 4, clients={}, schedule=sched)
    assert scheduled == [(N, 2, 90), (N, 3, 90), (N, 4, 90)]  # no 5th window

    a = ledger.get_recipient(notice, N, "a")
    assert a["state"] == "acknowledged"
    assert [h["channel"] for h in a["tier_history"] if h["state"] == "sent"] == ["rcs"]  # nothing after the tap

    b = ledger.get_recipient(notice, N, "b")
    assert [h["channel"] for h in b["tier_history"] if h["state"] == "sent"] == ["rcs", "whatsapp", "email"]
    assert b["state"] == "unreachable" and b["unreachable_reason"].startswith("no acknowledgment after rcs, whatsapp, email")


def test_recipient_without_any_channel_is_unreachable_at_stage_1_with_reason(notice):
    rule_engine.run_stage(notice, N, 1, clients={}, schedule=lambda *a: None)
    e = ledger.get_recipient(notice, N, "e")
    assert e["state"] == "unreachable" and e["unreachable_reason"] == "no channel on file"


def test_short_ladder_recipient_closes_when_its_only_window_elapses(notice):
    sched = lambda *a: None
    rule_engine.run_stage(notice, N, 1, clients={}, schedule=sched)
    d = ledger.get_recipient(notice, N, "d")  # email-only: one rung, sent at stage 1
    assert d["state"] == "sent" and [h["channel"] for h in d["tier_history"]] == ["email"]
    rule_engine.run_stage(notice, N, 2, clients={}, schedule=sched)  # its window has elapsed, no rung 2
    d = ledger.get_recipient(notice, N, "d")
    assert d["state"] == "unreachable" and d["unreachable_reason"] == "no acknowledgment after email"


def test_ack_after_unreachable_verdict_wins(notice):
    sched = lambda *a: None
    for s in (1, 2, 3, 4):
        rule_engine.run_stage(notice, N, s, clients={}, schedule=sched)
    assert ledger.get_recipient(notice, N, "b")["state"] == "unreachable"
    ack(notice, "b")
    assert ledger.get_recipient(notice, N, "b")["state"] == "acknowledged"


def test_failure_reason_flows_into_unreachable_reason(notice):
    sched = lambda *a: None
    rule_engine.run_stage(notice, N, 1, clients={}, schedule=sched)
    ledger.record_failure(notice, N, "b", "email", "Bounce", "bounce Permanent/NoEmail", "t")
    for s in (2, 3, 4):
        rule_engine.run_stage(notice, N, s, clients={}, schedule=sched)
    assert "bounce Permanent/NoEmail" in ledger.get_recipient(notice, N, "b")["unreachable_reason"]


def test_all_live_sends_failing_raises(tbl):
    ledger.put_meta(tbl, N, dict(META))
    for rid in ("x", "y"):
        ledger.put_recipient(tbl, N, rec(rid, live=True))

    class Boom:
        def send_rcs_message(self, **kw):
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "SendRcsMessage")

    with pytest.raises(RuntimeError, match="all 2 sends failed"):
        rule_engine.run_stage(tbl, N, 1, clients={"rcs": Boom()}, schedule=lambda *a: None)
    assert "send failed" in ledger.get_recipient(tbl, N, "x")["last_failure"]["reason"]


def test_unexpected_exception_is_not_swallowed(tbl):
    ledger.put_meta(tbl, N, dict(META))
    ledger.put_recipient(tbl, N, rec("x", live=True))

    class Bug:
        def send_rcs_message(self, **kw):
            raise KeyError("programming error")

    with pytest.raises(KeyError):
        rule_engine.run_stage(tbl, N, 1, clients={"rcs": Bug()}, schedule=lambda *a: None)


def test_delivered_and_read_recipients_are_not_called_unreachable_at_close(notice):
    sched = lambda *a: None
    rule_engine.run_stage(notice, N, 1, clients={}, schedule=sched)
    for rid, tier, ev in (("a", "delivered", "RCS_DELIVERED"), ("b", "read", "RCS_READ")):
        ledger.record_event(notice, {"notice_id": N, "recipient_id": rid, "channel": "rcs", "event": ev, "tier": tier,
                                     "provider_message_id": f"m-{rid}", "provider_timestamp": "t", "raw": {"x": 1}})
    for s in (2, 3, 4):
        rule_engine.run_stage(notice, N, s, clients={}, schedule=sched)
    assert ledger.get_recipient(notice, N, "a")["state"] == "delivered"
    assert ledger.get_recipient(notice, N, "b")["state"] == "read"
    assert ledger.get_recipient(notice, N, "c")["state"] == "unreachable"  # sent on every rung, never delivered


class CountingRcs:
    def __init__(self):
        self.sent = []

    def send_rcs_message(self, **kw):
        self.sent.append(kw["DestinationPhoneNumber"])
        return {"MessageId": f"m-{len(self.sent)}"}


def test_rerun_of_a_stage_does_not_double_send(tbl):
    ledger.put_meta(tbl, N, dict(META))
    ledger.put_recipient(tbl, N, rec("live-1", live=True))
    fake = CountingRcs()
    sched = []
    rule_engine.run_stage(tbl, N, 1, clients={"rcs": fake}, schedule=lambda *a: sched.append(a))
    rule_engine.run_stage(tbl, N, 1, clients={"rcs": fake}, schedule=lambda *a: sched.append(a))  # Lambda retry or Scheduler redelivery
    assert fake.sent == ["+1555"]


def test_stage_older_than_the_notice_is_skipped_entirely(tbl):
    ledger.put_meta(tbl, N, dict(META))
    ledger.put_recipient(tbl, N, rec("live-1", live=True))
    fake = CountingRcs()
    for s in (1, 2, 3, 4):
        rule_engine.run_stage(tbl, N, s, clients={"rcs": fake, "whatsapp": type("W", (), {"send_whatsapp_message": lambda self, **k: {"messageId": "w"}})(),
                                                  "email": type("E", (), {"send_email": lambda self, **k: {"MessageId": "e"}})()},
                              schedule=lambda *a: None)
    sent_before = len(fake.sent)
    out = rule_engine.run_stage(tbl, N, 1, clients={"rcs": fake}, schedule=lambda *a: None)  # stale redelivery after close
    assert out["skipped"] and len(fake.sent) == sent_before
    assert ledger.get_meta(tbl, N)["stage"] == 4 and ledger.get_meta(tbl, N)["status"] == "closed"


def test_all_sends_failing_still_schedules_the_next_window(tbl):
    ledger.put_meta(tbl, N, dict(META))
    ledger.put_recipient(tbl, N, rec("x", live=True))

    class Boom:
        def send_rcs_message(self, **kw):
            raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "arn:aws:iam::123456789012:role/x +15551234567"}}, "SendRcsMessage")

    sched = []
    with pytest.raises(RuntimeError):
        rule_engine.run_stage(tbl, N, 1, clients={"rcs": Boom()}, schedule=lambda *a: sched.append(a))
    assert sched == [(N, 2, 90)]  # the ladder moves on to the next channel
    reason = ledger.get_recipient(tbl, N, "x")["last_failure"]["reason"]
    assert reason == "send failed: ThrottlingException" and "123456789012" not in reason


def test_unreachable_verdict_never_overwrites_a_delivery_that_landed_in_between(notice):
    ledger.record_event(notice, {"notice_id": N, "recipient_id": "a", "channel": "rcs", "event": "RCS_DELIVERED", "tier": "delivered",
                                 "provider_message_id": "m", "provider_timestamp": "t", "raw": {"x": 1}})
    assert ledger.mark_unreachable(notice, N, "a", "stale verdict") is False
    assert ledger.get_recipient(notice, N, "a")["state"] == "delivered"


def test_schedule_names_are_valid_for_hostile_notice_ids():
    name = rule_engine.schedule_name("Recall: heater/HX 200 ü" + "x" * 100, 2)
    import re
    assert re.fullmatch(r"[0-9A-Za-z_.-]{1,64}", name) and name.endswith("-stage2")
