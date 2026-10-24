"""Lambda Function URL: read API for the dashboard plus the one-shot start.

Routes: GET /state, GET /report, POST /start. No login (the dashboard is the
monitoring UI of the automation); contact data never leaves the table.
"""
import json
import logging
import os
import time
from typing import Any, Dict, List

from agent import client as agent_client
from agent import notice_text
from api import clock, redact, report
from common import ledger
from engine import start_notice

DEFAULT_NOTICE = os.environ.get("DEFAULT_NOTICE_ID", "HX200-2026-10")
LIVE_EVENTS = 6
STATE_PROJECTION = "recipient_id, #s, by_channel, live_device, #seg, replay_events"
STATE_NAMES = {"#s": "state", "#seg": "segment"}  # both are DynamoDB reserved words
REPORT_PROJECTION = "recipient_id, #s, by_channel, live_device, #seg, unreachable_reason, recent_event_ids, replay_events"
START_TOKEN = os.environ.get("START_TOKEN", "")
SUMMARY_RETRY_SECONDS = 60
EXCEPTION_ROWS = 50  # the report lists the first rows; the full list stays in the ledger


def _json(status: int, body: Dict[str, Any]) -> Dict[str, Any]:
    return {"statusCode": status, "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
            "body": json.dumps(body, default=str)}


def _state_items(tbl, notice_id: str) -> List[Dict[str, Any]]:
    items = ledger.query_prefix(tbl, notice_id, "recipient#", STATE_PROJECTION, STATE_NAMES)
    for it in items:
        it["state"] = it.pop("state", "none")
    return items


def _live_rows(tbl, notice_id: str, meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    for rid in meta.get("live_ids", []):
        item = ledger.get_recipient(tbl, notice_id, rid)
        if item is None:
            continue
        history = []
        for h in item.get("tier_history", [])[-LIVE_EVENTS:]:
            ev = tbl.get_item(Key={"pk": ledger.notice_pk(notice_id), "sk": f"event#{h['event_id']}"}).get("Item") if h.get("event_id") else None
            history.append({**h, "provider_message_id": redact.mask_id(h.get("provider_message_id")), "raw": redact.public_raw(json.loads(ev["raw_json"])) if ev else None})
        rows.append({"recipient_id": rid, "label": meta.get("live_labels", {}).get(rid, rid), "state": item["state"],
                     "by_channel": item.get("by_channel", {}), "unreachable_reason": item.get("unreachable_reason"),
                     "feedback_status": item.get("feedback_status"), "history": history,
                     "clock": clock.next_step(item, meta)})
    return rows


def _public_texts(meta: Dict[str, Any]):
    """The stored notice texts with the validator's verdict, or None before Start wrote them."""
    texts, brief = meta.get("texts"), meta.get("brief")
    if not isinstance(texts, dict) or not isinstance(brief, dict):
        return None
    return notice_text.check_texts(texts, brief, meta.get("text_sources") or {})


def get_state(tbl, notice_id: str) -> Dict[str, Any]:
    meta = ledger.get_meta(tbl, notice_id)
    if meta is None:
        return {"status": "no_notice"}
    items = _state_items(tbl, notice_id)
    counts = report.tier_counts(items)
    return {
        "notice_id": notice_id, "status": meta.get("status", "ready"), "stage": int(meta.get("stage", 0)),
        "title": meta.get("title"), "total": len(items), "counts": counts, "label": report.bar_label(counts, len(items)),
        "channels": report.channel_breakdown(items), "footer": report.footer(items, meta),
        "text_sources": meta.get("text_sources"), "live": _live_rows(tbl, notice_id, meta),
        "texts": _public_texts(meta),
        "server_time": ledger.now_iso(),  # the dashboard counts down against this clock, not the viewer's
    }


def get_report(tbl, notice_id: str, summarize=agent_client.summarize_replies) -> Dict[str, Any]:
    meta = ledger.get_meta(tbl, notice_id)
    if meta is None:
        return {"status": "no_notice"}
    items = ledger.query_prefix(tbl, notice_id, "recipient#", REPORT_PROJECTION, STATE_NAMES)  # no history, no contact data
    for it in items:
        it["state"] = it.pop("state", "none")
    replies = [r["text"] for r in ledger.query_prefix(tbl, notice_id, "reply#")]
    themes = meta.get("reply_themes", [])
    retry_ok = time.time() - float(meta.get("summary_failed_at", 0)) > SUMMARY_RETRY_SECONDS
    if replies and meta.get("reply_count") != len(replies) and retry_ok:
        result = summarize(replies)
        themes = result["themes"]
        if result["error"] is None:
            ledger.update_meta(tbl, notice_id, {"reply_themes": themes, "reply_count": len(replies)})
        else:  # never cached as final, but not retried on every 5 s poll either
            ledger.update_meta(tbl, notice_id, {"summary_failed_at": int(time.time())})
    unmatched = sum(len(ledger.query_prefix(tbl, n, "unmatched#", "sk")) for n in {notice_id, "unknown"})
    out = report.build_report(items, meta, themes, unmatched)
    out["exception_total"] = len(out["exceptions"])
    out["exceptions"] = out["exceptions"][:EXCEPTION_ROWS]
    out["reply_count"] = len(replies)
    return out


def _start(tbl, notice_id: str, headers: Dict[str, str]) -> Dict[str, Any]:
    if START_TOKEN and headers.get("x-start-token") != START_TOKEN:
        return _json(403, {"error": "start token required"})
    try:
        result = start_notice.start(tbl, notice_id)
    except Exception:  # noqa: BLE001 - the claim is released by start(); tell the officer to press Start again
        logging.getLogger(__name__).exception("start failed")
        return _json(500, {"error": "start failed; press Start notice again"})
    return _json(result.pop("status"), result)


def handler(event, _context=None):
    method = event["requestContext"]["http"]["method"]
    path = event["rawPath"]
    params = event.get("queryStringParameters") or {}
    notice_id = params.get("notice") or DEFAULT_NOTICE
    tbl = ledger.table()
    if method == "GET" and path == "/state":
        return _json(200, get_state(tbl, notice_id))
    if method == "GET" and path == "/report":
        return _json(200, get_report(tbl, notice_id))
    if method == "POST" and path == "/start":
        return _start(tbl, notice_id, {k.lower(): v for k, v in (event.get("headers") or {}).items()})
    return _json(404, {"error": "not found"})
