"""Shared batch plumbing for the four consumers.

Events reach a consumer through SNS -> SQS (see template.yaml), so a burst is
buffered and the account's low Lambda concurrency is never exhausted. A record is
an SQS message whose body is the SNS notification; the plain SNS record shape is
also accepted so tests and manual invokes can call a handler directly.
"""
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Tuple

from common import ledger

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)  # the Lambda root logger defaults to WARNING, which hid every empty record
THREADS = 8  # records of one batch are independent: the ledger is idempotent and order-insensitive


def unwrap(record: Dict[str, Any]) -> Tuple[Dict[str, Any], str, str]:
    """(provider message, SNS timestamp, record id) from an SQS-wrapped or direct SNS record."""
    if "Sns" in record:
        sns = record["Sns"]
        return json.loads(sns["Message"]), sns.get("Timestamp", ""), sns.get("MessageId", "")
    notification = json.loads(record["body"])
    return json.loads(notification["Message"]), notification.get("Timestamp", ""), record["messageId"]


def make_handler(handle: Callable[[Any, Dict[str, Any], str], int]) -> Callable:
    """Build a Lambda handler from `handle(tbl, message, sns_timestamp) -> events processed`.

    Failed records are reported individually so only they are retried. If every record
    in the batch fails the error is raised: that is a broken consumer, not bad input.
    """
    def handler(event, _context=None):
        tbl = ledger.table()

        def one(record):
            message, ts, _ = unwrap(record)
            processed = handle(tbl, message, ts)
            if processed == 0:
                log.info("record produced no events; message keys: %s", sorted(message))
            return processed

        def safe(record):
            try:
                return one(record), None
            except Exception:  # noqa: BLE001 - reported per record below, never swallowed
                log.exception("record failed")
                return 0, record.get("messageId") or record.get("Sns", {}).get("MessageId", "")

        with ThreadPoolExecutor(max_workers=THREADS) as pool:
            results = list(pool.map(safe, event["Records"]))
        failures = [rid for _, rid in results if rid is not None]
        if failures and len(failures) == len(results):
            raise RuntimeError(f"all {len(failures)} records in the batch failed")
        return {"processed": sum(n for n, _ in results), "batchItemFailures": [{"itemIdentifier": f} for f in failures]}

    return handler
