"""Apply one normalized event end to end: correlate, record, remember failures, close the loop.

All four consumers call `process`; the replay path goes through the same consumers,
so there is one code path for live and replayed events.
"""
from typing import Any, Callable, Dict, Optional

from common import feedback, ledger


def _resolve(tbl, env: Dict[str, Any]) -> bool:
    """Fill notice_id/recipient_id from the message map when the payload lacks them."""
    if env.get("notice_id") and env.get("recipient_id"):
        return True
    mapped = ledger.lookup_message(tbl, env.get("lookup_id") or "")
    if mapped:
        env["notice_id"], env["recipient_id"] = mapped["notice_id"], mapped["recipient_id"]
        return True
    return False


def process(tbl, env: Dict[str, Any], close_loop: Optional[Callable] = None) -> Dict[str, Any]:
    close_loop = close_loop or feedback.close_loop  # resolved at call time so tests can patch the module
    resolved = _resolve(tbl, env)
    if env["tier"] == "reply":
        if not resolved:
            return ledger._store_unmatched(tbl, env, ledger.event_id(env), env.get("notice_id"))
        ledger.store_reply(tbl, env["notice_id"], env["recipient_id"], env["channel"],
                           env.get("reply_text", ""), ledger.event_id(env), env.get("provider_timestamp", ""))
        return {"status": "reply_stored"}

    result = ledger.record_event(tbl, env)
    if result["status"] in ("duplicate", "unmatched"):
        return result

    if env["tier"] == "failed":
        ledger.record_failure(tbl, env["notice_id"], env["recipient_id"], env["channel"], env["event"],
                              env.get("failure_reason", env["event"]), env.get("provider_timestamp", ""))
    elif env["tier"] == "acknowledged" and env["channel"] == "rcs" and result["status"] == "applied":
        item = ledger.get_recipient(tbl, env["notice_id"], env["recipient_id"])
        result["feedback"] = close_loop(tbl, item)
    return result
