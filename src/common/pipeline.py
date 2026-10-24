"""Apply one normalized event end to end: correlate, record, remember failures, close the loop.

All four consumers call `process`; the replay path goes through the same consumers,
so there is one code path for live and replayed events.
"""
import logging
from typing import Any, Callable, Dict, Optional

from common import feedback, ledger
from common.tiers import UNPARSED

log = logging.getLogger(__name__)


def _resolve(tbl, env: Dict[str, Any]) -> bool:
    """Fill notice_id/recipient_id when the payload lacks them: message map by id, by the AWS send id, then the phone map.

    A WhatsApp status that resolved without the map knowing its own id teaches the map that id,
    so the rest of that message's statuses match directly.
    """
    own = ledger.lookup_message(tbl, env["lookup_id"]) if env.get("lookup_id") else None
    if not (env.get("notice_id") and env.get("recipient_id")):
        mapped = own or next((m for m in (ledger.lookup_message(tbl, i) for i in env.get("alt_lookup_ids", [])) if m), None) \
            or ledger.lookup_phone(tbl, env.get("lookup_phone"))
        if not mapped:
            return False
        env["notice_id"], env["recipient_id"] = mapped["notice_id"], mapped["recipient_id"]
    if env.get("learn_lookup") and env.get("lookup_id") and own is None and not _is_replay(env):
        ledger.register_message(tbl, env["lookup_id"], env["notice_id"], env["recipient_id"], env["channel"], keep_existing=True)
    return True


def _is_replay(env: Dict[str, Any]) -> bool:
    """Replayed events carry `replay-` ids (see seed/corpus.py); live provider ids never do."""
    return any(str(env.get(k) or "").startswith("replay-") for k in ("provider_message_id", "lookup_id"))


def process(tbl, env: Dict[str, Any], close_loop: Optional[Callable] = None) -> Dict[str, Any]:
    close_loop = close_loop or feedback.close_loop  # resolved at call time so tests can patch the module
    env["replayed"] = env.get("replayed", False) or _is_replay(env)
    if env["tier"] == UNPARSED:
        return ledger._store_unmatched(tbl, env, ledger.event_id(env), None)
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
        try:
            result["feedback"] = close_loop(tbl, item)
        except Exception:  # the acknowledgment is committed: a failing side call must not fail the batch, a redelivery would skip it as a duplicate
            log.exception("loop closer failed after the acknowledgment for %s/%s", env["notice_id"], env["recipient_id"])
            result["feedback"] = "failed:unrecorded"
    return result
