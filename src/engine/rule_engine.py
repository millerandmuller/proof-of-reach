"""F6: run one stage of the ladder for a notice, then schedule the next window.

Invoked by EventBridge Scheduler at the end of each window (and directly by
start_notice for stage 1). Escalation, windows and stop are plain code with the
ledger as the only input; no model is consulted.
"""
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from common import config, ledger
from engine import ladder
from senders import dispatch

log = logging.getLogger(__name__)
MAX_WORKERS = 16


def schedule_stage(notice_id: str, stage: int, window_seconds: int, scheduler=None) -> str:
    """Create a one-time schedule that re-invokes this function for `stage`."""
    scheduler = scheduler or boto3.client("scheduler", region_name=config.REGION)
    when = (datetime.now(timezone.utc) + timedelta(seconds=window_seconds)).strftime("%Y-%m-%dT%H:%M:%S")
    name = f"por-{notice_id}-stage{stage}"
    scheduler.create_schedule(
        Name=name,
        ScheduleExpression=f"at({when})",
        ScheduleExpressionTimezone="UTC",
        FlexibleTimeWindow={"Mode": "OFF"},
        ActionAfterCompletion="DELETE",
        Target={
            "Arn": config.ENGINE_FUNCTION_ARN,
            "RoleArn": config.SCHEDULER_ROLE_ARN,
            "Input": json.dumps({"notice_id": notice_id, "stage": stage}),
        },
    )
    return name


def _act_on(tbl, meta: Dict[str, Any], stage: int, clients: Dict[str, Any], item: Dict[str, Any]) -> str:
    """Handle one recipient. Returns sent | skipped | unreachable | failed."""
    fresh = ledger.get_recipient(tbl, item["notice_id"], item["recipient_id"])  # state may have moved since the listing
    channel = ladder.rung_for(fresh, stage)
    if channel is not None:
        try:
            dispatch.send_rung(tbl, meta, fresh, channel, stage, clients)
            return "sent"
        except (ClientError, BotoCoreError) as err:  # expected provider errors only; anything else is a bug and must be loud
            ledger.record_failure(tbl, fresh["notice_id"], fresh["recipient_id"], channel, "SEND_FAILED",
                                  f"send failed: {err}", ledger.now_iso())
            log.warning("send failed for %s on %s: %s", fresh["recipient_id"], channel, err)
            return "failed"
    if ladder.needs_verdict(fresh, stage) or (stage >= ladder.CLOSE_STAGE and fresh["state"] not in ("acknowledged", "unreachable")):
        ledger.mark_unreachable(tbl, fresh["notice_id"], fresh["recipient_id"], ladder.unreachable_reason(fresh, stage))
        return "unreachable"
    return "skipped"


def run_stage(tbl, notice_id: str, stage: int, clients: Optional[Dict[str, Any]] = None,
              schedule: Callable[[str, int, int], Any] = schedule_stage) -> Dict[str, Any]:
    meta = ledger.get_meta(tbl, notice_id)
    if meta is None:
        raise ValueError(f"unknown notice {notice_id}")
    clients = clients if clients is not None else dispatch.default_clients()
    recipients = ledger.query_prefix(tbl, notice_id, "recipient#")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        outcomes: List[str] = list(pool.map(lambda it: _act_on(tbl, meta, stage, clients, it), recipients))

    counts = {k: outcomes.count(k) for k in ("sent", "skipped", "unreachable", "failed")}
    attempted = counts["sent"] + counts["failed"]
    ledger.put_meta(tbl, notice_id, {**meta, "stage": stage, "stage_counts": counts, "stage_at": ledger.now_iso()})
    if attempted and counts["failed"] == attempted:
        # Every send failed: that is a broken stage (config, quota, permissions), not noise.
        raise RuntimeError(f"stage {stage} of {notice_id}: all {attempted} sends failed")
    if stage < ladder.CLOSE_STAGE:
        schedule(notice_id, stage + 1, int(meta.get("window_seconds", config.WINDOW_SECONDS)))
    else:
        ledger.put_meta(tbl, notice_id, {**meta, "stage": stage, "status": "closed", "closed_at": ledger.now_iso(), "stage_counts": counts})
    return {"notice_id": notice_id, "stage": stage, **counts}


def handler(event, _context=None):
    return run_stage(ledger.table(), event["notice_id"], int(event["stage"]))
