import json

from api import handler
from common import ledger

N = "N1"
BRIEF = {"product": "heater HX-200", "hazard": "can overheat", "hazard_key": "overheat", "action": "unplug it",
         "action_key": "unplug", "contact": "0800"}


def seed(tbl):
    ledger.put_meta(tbl, N, {"status": "ready", "brief": BRIEF, "live_ids": ["live"], "title": "Recall",
                             "replay_provenance": "recorded"})
    ledger.put_recipient(tbl, N, {"recipient_id": "live", "rcs_capable": True, "phone": "+1", "email": "a@x.test", "live_device": True})
    ledger.put_recipient(tbl, N, {"recipient_id": "r1", "rcs_capable": True, "phone": "+2", "live_device": False})


def call(method, path, **params):
    return handler.handler({"requestContext": {"http": {"method": method}}, "rawPath": path, "queryStringParameters": params or None})


def test_start_is_one_shot_and_uses_writer_texts(tbl):
    seed(tbl)
    started = []
    # start() binds its default collaborators at import time, so pass explicit ones
    r1 = handler.start_notice.start(tbl, N, writer=lambda b: ({"consumer": {}}, {"consumer.rcs": "default"}, "no runtime"),
                                    invoke_engine=lambda nid, st: started.append((nid, st)))
    r2 = handler.start_notice.start(tbl, N, writer=lambda b: ({}, {}, None), invoke_engine=lambda nid, st: started.append((nid, st)))
    assert r1["ok"] and r1["status"] == 202 and r2 == {"ok": False, "error": "notice already started", "status": 409}
    assert started == [(N, 1)]
    meta = ledger.get_meta(tbl, N)
    assert meta["status"] == "running" and meta["text_error"] == "no runtime" and meta["started_at"]


def test_state_before_start_is_all_not_started_and_hides_contacts(tbl):
    seed(tbl)
    resp = call("GET", "/state", notice=N)
    body = json.loads(resp["body"])
    assert resp["statusCode"] == 200 and body["counts"]["not_started"] == 2 and body["label"] == "0 % acknowledged · 0 % sent"
    assert "+1" not in resp["body"] and "a@x.test" not in resp["body"]


def test_state_shows_live_row_with_raw_event(tbl):
    seed(tbl)
    ledger.record_event(tbl, {"notice_id": N, "recipient_id": "live", "channel": "rcs", "event": "RCS_READ", "tier": "read",
                              "provider_message_id": "m1", "provider_timestamp": "t", "raw": {"eventType": "RCS_READ"}})
    body = json.loads(call("GET", "/state", notice=N)["body"])
    live = body["live"][0]
    assert live["state"] == "read" and live["history"][0]["raw"] == {"eventType": "RCS_READ"}


def test_report_endpoint_adds_up_and_summarizes_replies_once(tbl, monkeypatch):
    seed(tbl)
    ledger.store_reply(tbl, N, "live", "rcs", "where is my refund?", "e1", "t")
    calls = []

    def fake(replies):
        calls.append(replies)
        return {"themes": [{"label": "refund", "replies": [1]}], "error": None}

    monkeypatch.setattr(handler.agent_client, "summarize_replies", fake)
    out = handler.get_report(tbl, N, summarize=fake)
    out2 = handler.get_report(tbl, N, summarize=fake)
    assert out["total"] == 2 and sum(out["counts"].values()) == 2 and out["themes"] == out2["themes"]
    assert len(calls) == 1  # cached by reply count


def test_unknown_route_and_missing_notice(tbl):
    assert call("GET", "/nope")["statusCode"] == 404
    assert json.loads(call("GET", "/state", notice="missing")["body"]) == {"status": "no_notice"}


def test_state_before_start_has_no_texts_and_no_clock(tbl):
    seed(tbl)
    body = json.loads(call("GET", "/state", notice=N)["body"])
    assert body["texts"] is None and body["live"][0]["clock"] is None and body["server_time"].endswith("Z")


def test_state_carries_the_rule_clock_and_the_checked_texts_once_running(tbl):
    seed(tbl)
    texts, sources = handler.notice_text.merge_with_defaults({}, BRIEF)
    ledger.update_meta(tbl, N, {"status": "running", "stage": 1, "stage_at": "2026-10-26T12:00:00Z", "window_seconds": 90,
                                "texts": texts, "text_sources": sources})
    assert json.loads(call("GET", "/state", notice=N)["body"])["live"][0]["clock"]["tried"] == []  # stage 1 recorded, but no send recorded for this device
    ledger.record_event(tbl, {"notice_id": N, "recipient_id": "live", "channel": "rcs", "event": "SENT", "tier": "sent",
                              "provider_message_id": "m-sent", "provider_timestamp": "2026-10-26T12:00:00Z", "raw": {}, "stage": 1})
    resp = call("GET", "/state", notice=N)
    body = json.loads(resp["body"])
    step = body["live"][0]["clock"]
    assert step["next_stage"] == 2 and step["due_at"] == "2026-10-26T12:01:30Z" and step["tried"] == ["rcs"]
    assert body["texts"]["consumer"]["rcs"]["problems"] == [] and body["texts"]["dealer"]["email"]["source"] == "default"
    assert "+1" not in resp["body"] and "a@x.test" not in resp["body"]


def test_state_history_carries_one_note_per_cause_and_the_whatsapp_status_chip(tbl):
    seed(tbl)

    def event(channel, name, tier, mid, raw, **kw):
        ledger.record_event(tbl, {"notice_id": N, "recipient_id": "live", "channel": channel, "event": name, "tier": tier,
                                  "provider_message_id": mid, "provider_timestamp": f"2026-10-26T12:00:{len(mid):02d}Z", "raw": raw, **kw})

    event("rcs", "RCS_READ", "read", "m-read", {"eventType": "RCS_READ"})
    event("rcs", "RCS_DELIVERED", "delivered", "m-late", {"eventType": "RCS_DELIVERED"})  # below the row's tier
    event("rcs", "RCS_READ", "read", "m-twice", {"eventType": "RCS_READ"})  # same tier again
    event("email", "Open", "read", "m-bot", {"eventType": "Open"}, is_bot=True)
    entry = {"changes": [{"value": {"statuses": [{"id": "w", "status": "failed", "errors": [{"code": 131031}]}]}}]}
    event("whatsapp", "failed", "failed", "w-failed", {"whatsAppWebhookEntry": json.dumps(entry)})
    body = json.loads(call("GET", "/state", notice=N)["body"])
    notes = {h["provider_message_id"]: h["note"] for h in body["live"][0]["history"]}
    assert notes == {"m-read": "", "m-late": "row already at read", "m-twice": "already at this tier",
                     "m-bot": "bot-flagged, not counted", "w-failed": ""}
    chips = {h["provider_message_id"]: h["chip"] for h in body["live"][0]["history"]}
    assert chips["w-failed"] == "failed · error 131031" and chips["m-read"] is None


def test_an_event_without_a_provider_payload_is_not_listed_as_one(tbl):
    seed(tbl)
    for name, tier, mid, raw in (("SENT", "sent", "m-send", {}), ("RCS_DELIVERED", "delivered", "m-del", {"eventType": "RCS_DELIVERED"})):
        ledger.record_event(tbl, {"notice_id": N, "recipient_id": "live", "channel": "rcs", "event": name, "tier": tier,
                                  "provider_message_id": mid, "provider_timestamp": f"2026-10-26T12:00:{len(mid):02d}Z", "raw": raw})
    history = {h["provider_message_id"]: h for h in json.loads(call("GET", "/state", notice=N)["body"])["live"][0]["history"]}
    assert history["m-send"]["raw"] is None  # the engine's own send: the page lists only real payloads
    assert history["m-del"]["raw"] == {"eventType": "RCS_DELIVERED"}


def test_history_notes_are_fixed_at_event_time_and_old_entries_get_none(tbl):
    seed(tbl)

    def event(tier, mid, ts):
        ledger.record_event(tbl, {"notice_id": N, "recipient_id": "live", "channel": "rcs", "event": mid, "tier": tier,
                                  "provider_message_id": mid, "provider_timestamp": ts, "raw": {"eventType": mid}})

    event("delivered", "d1", "2026-10-26T12:00:01Z")
    event("delivered", "d2", "2026-10-26T12:00:02Z")  # same tier, row at delivered
    first = {h["provider_message_id"]: h["note"] for h in json.loads(call("GET", "/state", notice=N)["body"])["live"][0]["history"]}
    event("read", "r1", "2026-10-26T12:00:03Z")
    event("acknowledged", "a1", "2026-10-26T12:00:04Z")
    later = {h["provider_message_id"]: h["note"] for h in json.loads(call("GET", "/state", notice=N)["body"])["live"][0]["history"]}
    assert first["d2"] == "already at this tier" and later["d2"] == "already at this tier"  # the row climbed, the note did not move
    item = ledger.get_recipient(tbl, N, "live")
    item["tier_history"] = [{k: v for k, v in h.items() if k != "row_state"} for h in item["tier_history"]]  # as written before this change
    tbl.put_item(Item=item)
    old = {h["provider_message_id"]: h["note"] for h in json.loads(call("GET", "/state", notice=N)["body"])["live"][0]["history"]}
    assert old["d2"] == ""
