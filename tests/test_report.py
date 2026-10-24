from datetime import datetime, timezone

from api import report


def item(rid, state, by_channel=None, live=False, reason=None, history=None, replay_events=0):
    return {"recipient_id": rid, "state": state, "by_channel": by_channel or {}, "live_device": live,
            "unreachable_reason": reason, "tier_history": history or [], "segment": "consumer",
            "replay_events": replay_events}


ITEMS = [
    item("a", "acknowledged", {"rcs": "acknowledged"}, live=True, history=[{"event_id": "e1"}]),
    item("b", "read", {"rcs": "sent", "whatsapp": "read"}, replay_events=2),
    item("c", "delivered", {"email": "delivered"}, replay_events=1),
    item("d", "sent", {"rcs": "sent"}, replay_events=1),
    item("e", "unreachable", {"rcs": "sent"}, reason="no acknowledgment after rcs", replay_events=1),
    item("f", "none", replay_events=1),
]
META = {"started_at": "2026-10-26T12:00:00Z", "closed_at": "2026-10-26T18:00:00Z", "status": "closed", "replay_provenance": "recorded"}


def test_buckets_add_up_and_label_separates_ack_from_sent():
    counts = report.tier_counts(ITEMS)
    assert sum(counts.values()) == len(ITEMS) and counts["acknowledged"] == 1
    assert report.bar_label(counts, 6) == "17 % acknowledged · 83 % sent"


def test_channel_breakdown_counts_best_tier_per_channel():
    ch = report.channel_breakdown(ITEMS)
    assert ch["rcs"] == {"sent": 4, "delivered": 1, "read": 1, "acknowledged": 1}
    assert ch["whatsapp"]["read"] == 1 and ch["email"]["delivered"] == 1


def test_report_headline_last_line_and_footer():
    r = report.build_report(ITEMS, META, themes=[], now=datetime(2026, 10, 27, tzinfo=timezone.utc))
    assert r["headline"] == "acknowledged 1 of 6 after 6 h 00 min"
    assert r["last_line"] == "1 people: unreachable, letter needed"
    assert r["footer"] == "6 recipients · 1 live devices · 5 replayed from recorded events"
    assert [row["recipient_id"] for row in r["exceptions"]] == ["e", "b", "c", "d", "f"]
    assert r["reasons"] == [{"reason": "no acknowledgment after rcs", "count": 1}]


def test_open_notice_does_not_claim_letters_and_simulated_footer_is_honest():
    meta = {"started_at": "2026-10-26T12:00:00Z", "status": "running"}
    r = report.build_report(ITEMS, meta, themes=[], now=datetime(2026, 10, 26, 12, 1, 30, tzinfo=timezone.utc))
    assert r["headline"].endswith("after 1 min 30 s") and "have not acknowledged yet" in r["last_line"]
    assert "simulated events" in r["footer"]


def test_exception_rows_use_the_cached_event_ids_when_history_is_not_loaded():
    row = {"recipient_id": "a", "state": "read", "by_channel": {}, "live_device": False, "unreachable_reason": None,
           "recent_event_ids": ["e1", "e2"], "segment": "consumer"}  # the report projection carries no tier_history
    assert report.exception_rows([row])[0]["event_ids"] == ["e1", "e2"]


def test_footer_says_replay_not_run_until_replay_events_exist():
    simulated_send_only = [item("a", "acknowledged", live=True)] + [item(c, "sent") for c in "bcde"]  # the engine's simulated send is not a replay event
    assert report.footer(simulated_send_only, {"replay_provenance": "simulated"}) == "5 recipients · 1 live devices · 4 simulated, replay not run"
    assert report.footer(simulated_send_only, {"replay_provenance": "recorded"}).endswith("4 recorded, replay not run")


def test_footer_after_replay_and_while_partly_replayed():
    full = [item("a", "acknowledged", live=True)] + [item(c, "read", replay_events=1) for c in "bcde"]
    assert report.footer(full, {}) == "5 recipients · 1 live devices · 4 replayed from simulated events"
    partial = [item("a", "acknowledged", live=True), item("b", "read", replay_events=3)] + [item(c, "sent") for c in "cde"]
    assert report.footer(partial, {}) == "5 recipients · 1 live devices · 1 replayed from simulated events · 3 not yet replayed"
