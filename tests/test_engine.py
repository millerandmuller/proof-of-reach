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
    ledger.record_failure(notice, N, "b", "email", "Bounce", "email Permanent/NoEmail", "t")
    for s in (2, 3, 4):
        rule_engine.run_stage(notice, N, s, clients={}, schedule=sched)
    assert "email Permanent/NoEmail" in ledger.get_recipient(notice, N, "b")["unreachable_reason"]


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
