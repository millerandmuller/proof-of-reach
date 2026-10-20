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
