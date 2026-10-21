"""Examiner findings: contact data in /state, start token, failed start, report robustness."""
import json
from datetime import datetime, timezone

import pytest

from agent import notice_text as nt
from api import handler, redact, report
from common import ledger
from seed import corpus

N = "N1"
BRIEF = {"product": "heater HX-200", "hazard": "can overheat", "hazard_key": "overheat", "action": "unplug it",
         "action_key": "unplug", "contact": "0800"}
PHONE, MAIL = "+15559876543", "jane.doe@corp.example"


def planted(raw: dict) -> str:
    return json.dumps(redact.public_raw(raw))


def test_redaction_removes_phone_numbers_and_mailboxes_from_every_provider_shape():
    rcs_status = {"eventType": "RCS_READ", "eventTimestamp": 1, "messageId": "m", "messageStatus": "READ",
                  "destinationPhoneNumber": PHONE, "originationPhoneNumber": "rcs-abc", "rcsBusinessId": "agent"}
    rcs_tap = {"originationNumber": PHONE, "destinationNumber": "rcs-agent", "inboundMessageId": "in-1",
               "messageBody": json.dumps({"type": "SUGGESTION", "text": "Jane Doe tapped", "postbackData": "por:v1:N:r:ack"})}
    rcs_reply = {"originationNumber": PHONE, "messageBody": f"call me on {PHONE}", "inboundMessageId": "in-2"}
    wa = {"whatsAppWebhookEntry": json.dumps({"id": "1", "changes": [{"field": "messages", "value": {
        "metadata": {"display_phone_number": PHONE}, "contacts": [{"profile": {"name": "Jane Doe"}, "wa_id": PHONE}],
        "statuses": [{"id": "wamid.A", "status": "read", "timestamp": "1", "recipient_id": PHONE}],
        "messages": [{"from": PHONE, "id": "wamid.B", "type": "text", "text": {"body": f"mail {MAIL}"}, "timestamp": "2"}]}}]}),
        "context": {"MetaWabaIds": [{"arn": "arn:aws:social-messaging:us-east-1:123456789012:waba/x"}]}, "aws_account_id": "123456789012"}
    ses = {"eventType": "Click", "mail": {"messageId": "s1", "timestamp": "t", "source": "notice@x.example", "destination": [MAIL],
                                           "headers": [{"name": "To", "value": MAIL}], "commonHeaders": {"to": [MAIL]}},
           "click": {"timestamp": "t", "link": "https://x/ack.html?p=por:v1:N:r:ack", "ipAddress": "203.0.113.9", "userAgent": "Mozilla", "isBotEvent": "Unlikely"}}
    for raw in (rcs_status, rcs_tap, rcs_reply, wa, ses):
        out = planted(raw)
        for secret in (PHONE, MAIL, "Jane Doe", "203.0.113.9", "123456789012", "notice@x.example", "rcs-abc"):
            assert secret not in out, (secret, out)
    assert json.loads(planted(rcs_tap))["messageBody"] == {"type": "SUGGESTION", "postbackData": "por:v1:N:r:ack"}
    assert json.loads(planted(rcs_status))["eventType"] == "RCS_READ"  # the useful fields survive
    assert "statuses" in planted(wa) and "wamid.A" in planted(wa)


def test_replayed_corpus_payloads_also_pass_through_redaction_clean():
    recipients = corpus.build_recipients(60, [])
    plan = corpus.plan_outcomes(recipients)
    events = corpus.build_corpus(N, recipients, plan, datetime(2026, 10, 26, tzinfo=timezone.utc), 90)
    for e in events:
        assert "15550000000" not in planted(e["message"])


def seed(tbl):
    ledger.put_meta(tbl, N, {"status": "ready", "brief": BRIEF, "live_ids": ["live"]})
    ledger.put_recipient(tbl, N, {"recipient_id": "live", "rcs_capable": True, "phone": PHONE, "email": MAIL, "live_device": True})
    ledger.register_message(tbl, "rcs-1", N, "live", "rcs")
    ledger.record_event(tbl, {"notice_id": N, "recipient_id": "live", "channel": "rcs", "event": "RCS_READ", "tier": "read",
                              "provider_message_id": "rcs-1", "provider_timestamp": "t",
                              "raw": {"eventType": "RCS_READ", "messageId": "rcs-1", "destinationPhoneNumber": PHONE}})


def test_state_endpoint_never_returns_the_live_devices_contact_data(tbl):
    seed(tbl)
    body = json.dumps(handler.get_state(tbl, N))
    assert PHONE not in body and MAIL not in body and "RCS_READ" in body
    assert PHONE not in json.dumps(handler.get_report(tbl, N, summarize=lambda r: {"themes": [], "error": None}))


def call(method, path, headers=None):
    return handler.handler({"requestContext": {"http": {"method": method}}, "rawPath": path, "headers": headers or {},
                            "queryStringParameters": {"notice": N}})


def test_start_requires_the_token_when_configured(tbl, monkeypatch):
    seed(tbl)
    monkeypatch.setattr(handler, "START_TOKEN", "t0k3n")
    assert call("POST", "/start")["statusCode"] == 403
    assert call("POST", "/start", {"X-Start-Token": "wrong"})["statusCode"] == 403
    assert ledger.get_meta(tbl, N)["status"] == "ready"  # a refused start changes nothing


def test_failed_start_releases_the_claim_so_start_can_be_pressed_again(tbl):
    from engine import start_notice
    seed(tbl)

    def boom(nid, stage):
        raise RuntimeError("engine invoke failed")

    with pytest.raises(RuntimeError):
        start_notice.start(tbl, N, writer=lambda b: (nt.default_texts(b), {}, None), invoke_engine=boom)
    assert ledger.get_meta(tbl, N)["status"] == "ready"
    ok = start_notice.start(tbl, N, writer=lambda b: (nt.default_texts(b), {}, None), invoke_engine=lambda *a: None)
    assert ok["ok"] is True


def test_start_endpoint_turns_an_unexpected_error_into_a_clear_500(tbl, monkeypatch):
    seed(tbl)
    monkeypatch.setattr(handler.start_notice, "start", lambda tbl, nid: (_ for _ in ()).throw(RuntimeError("x")))
    resp = call("POST", "/start")
    assert resp["statusCode"] == 500 and "press Start notice again" in resp["body"]


def test_writer_returning_garbage_shapes_falls_back_without_raising():
    for garbage in ({"consumer": "x"}, {"consumer": {"rcs": "y"}}, {"consumer": None}, [], "text", {"dealer": {"email": []}}):
        texts, sources = nt.merge_with_defaults(garbage, BRIEF)
        assert nt.validate_texts(texts, BRIEF) == {}
        assert set(sources.values()) == {"default"}


def test_summarizer_theme_output_is_cleaned():
    themes = [{"label": "refund", "replies": [1, 2.5, True, 99, 2]}, {"label": 7, "replies": [1]}, "junk", {"label": "x", "replies": []}]
    assert nt.clean_themes(themes, 3) == [{"label": "refund", "replies": [1, 2]}]
    assert nt.clean_themes("nope", 3) == []


def test_failed_summary_is_not_cached_and_is_retried(tbl):
    seed(tbl)
    ledger.store_reply(tbl, N, "live", "rcs", "where is my refund?", "e1", "t")
    calls = []

    def flaky(replies):
        calls.append(1)
        return {"themes": [], "error": "down"} if len(calls) == 1 else {"themes": [{"label": "refund", "replies": [1]}], "error": None}

    assert handler.get_report(tbl, N, summarize=flaky)["themes"] == []
    assert handler.get_report(tbl, N, summarize=flaky)["themes"] == [{"label": "refund", "replies": [1]}]
    assert len(calls) == 2


def test_report_caps_the_exception_list_and_counts_unmatched_in_both_partitions(tbl):
    ledger.put_meta(tbl, N, {"status": "running", "brief": BRIEF})
    for i in range(80):
        ledger.put_recipient(tbl, N, {"recipient_id": f"r{i:03d}", "rcs_capable": True, "phone": "+1", "live_device": False})
    ledger._store_unmatched(tbl, {"channel": "rcs", "event": "X", "raw": {}}, "e1", N)
    ledger._store_unmatched(tbl, {"channel": "rcs", "event": "X", "raw": {}}, "e2", None)  # lands under notice#unknown
    out = handler.get_report(tbl, N, summarize=lambda r: {"themes": [], "error": None})
    assert len(out["exceptions"]) == handler.EXCEPTION_ROWS and out["exception_total"] == 80
    assert out["unmatched_events"] == 2


def test_one_acknowledgment_never_reads_as_zero_percent():
    counts = {"acknowledged": 1, "read": 0, "delivered": 0, "sent": 1999, "unreachable": 0, "not_started": 0}
    assert report.bar_label(counts, 2000) == "<1 % acknowledged · 100 % sent"
