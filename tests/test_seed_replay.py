"""End to end without AWS: seed -> engine stages -> replay corpus through the real consumers -> ledger."""
import json
from datetime import datetime, timezone

from api import report
from common import ledger
from consumers import rcs_events, rcs_twoway, ses_events, whatsapp_events
from engine import rule_engine
from seed import corpus

N = "REPLAY-1"
BRIEF = {"product": "heater HX-200", "hazard": "overheat", "hazard_key": "overheat", "action": "unplug", "action_key": "unplug", "contact": "0800"}
TEXTS = {"rcs": {"title": "t", "body": "b"}, "whatsapp": {"body": "b"}, "email": {"subject": "s", "body": "b"}}
HANDLERS = {"rcs_events": rcs_events.handler, "rcs_twoway": rcs_twoway.handler, "whatsapp": whatsapp_events.handler, "ses": ses_events.handler}


def deliver(event):
    HANDLERS[event["topic"]]({"Records": [{"Sns": {"Message": json.dumps(event["message"]), "Timestamp": "2026-10-26T12:00:00.000Z"}}]})


def run(tbl, n, live=()):
    recipients = corpus.build_recipients(n, list(live))
    replayed = [r for r in recipients if not r["live_device"]]
    plan = corpus.plan_outcomes(replayed)
    ledger.put_meta(tbl, N, {"status": "running", "texts": {"consumer": TEXTS}, "ack_label": "Device off", "window_seconds": 90,
                             "brief": BRIEF, "started_at": "2026-10-26T12:00:00Z", "replay_provenance": corpus.PROVENANCE})
    for r in recipients:
        ledger.put_recipient(tbl, N, r)
    events = corpus.build_corpus(N, replayed, plan, datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc), 90)
    for stage in (1, 2, 3, 4):
        rule_engine.run_stage(tbl, N, stage, clients={}, schedule=lambda *a: None)
        for ev in (e for e in events if e["stage"] == stage):
            deliver(ev)
    return recipients, plan, events


def test_replay_reproduces_planned_outcomes_through_real_consumers(tbl):
    recipients, plan, events = run(tbl, 150)
    items = ledger.query_prefix(tbl, N, "recipient#")
    counts = report.tier_counts(items)
    expected = corpus.expected_counts(plan)
    assert counts["acknowledged"] == expected["acknowledged"]
    assert counts["read"] == expected["read"]
    assert counts["delivered"] == expected["delivered"]
    assert counts["unreachable"] == expected["unreachable"]
    assert counts["sent"] == 0 and counts["not_started"] == 0
    assert sum(counts.values()) == 150
    assert not ledger.query_prefix(tbl, N, "unmatched#")  # every replayed event found its row


def test_outcome_shares_match_the_demo_script_for_2000(tbl):
    recipients = corpus.build_recipients(2000, [{"recipient_id": "live-a", "live_device": True}] * 0)
    plan = corpus.plan_outcomes(recipients)
    expected = corpus.expected_counts(plan)
    assert abs(expected["acknowledged"] - 1643) <= 2 and abs(expected["read"] - 212) <= 2
    assert abs(expected["delivered"] - 98) <= 2 and abs(expected["unreachable"] - 47) <= 2


def test_replayed_rows_are_flagged_and_unreachable_reasons_are_specific(tbl):
    run(tbl, 150)
    items = ledger.query_prefix(tbl, N, "recipient#")
    assert all(h["replayed"] for i in items for h in i["tier_history"])
    reasons = {i["unreachable_reason"] for i in items if i["state"] == "unreachable"}
    assert "no channel on file" in reasons or any("no acknowledgment" in r for r in reasons)
    assert all(r and "email email" not in r for r in reasons)


def test_acknowledged_recipient_receives_nothing_after_the_ack_rung(tbl):
    run(tbl, 150)
    for i in ledger.query_prefix(tbl, N, "recipient#"):
        if i["state"] != "acknowledged":
            continue
        ack_idx = next(k for k, h in enumerate(i["tier_history"]) if h["state"] == "acknowledged" and h["counted"])
        assert not [h for h in i["tier_history"][ack_idx + 1:] if h["event"] == "SENT"]
