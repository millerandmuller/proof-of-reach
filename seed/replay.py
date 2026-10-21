"""Replay player: publishes the corpus to the real SNS topics so the real consumers process it.

    python seed/replay.py --stack proof-of-reach

Run it right after pressing "Start notice". For each stage it waits until the engine
has finished sending that stage, then releases that stage's events spread across the window.
Nothing is written to the ledger directly.
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import boto3

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "..", "src"))
sys.path.insert(0, os.path.join(ROOT, ".."))

from common import config, ledger  # noqa: E402
from seed import corpus, seed as seed_mod, stack  # noqa: E402

TOPIC_KEYS = {"rcs_events": "RcsEventsTopicArn", "rcs_twoway": "RcsTwoWayTopicArn", "whatsapp": "WhatsAppTopicArn", "ses": "SesTopicArn"}


def wait_for_stage(tbl, notice_id: str, stage: int, timeout: float) -> bool:
    """True once the engine reports `stage` finished (meta.stage is written after all sends)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        meta = ledger.get_meta(tbl, notice_id) or {}
        if int(meta.get("stage", 0)) >= stage:
            return True
        time.sleep(1)
    return False


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stack", default="proof-of-reach")
    ap.add_argument("--region", default=config.REGION)
    ap.add_argument("--notice", default=seed_mod.NOTICE_ID)
    ap.add_argument("--count", type=int, default=2000)
    ap.add_argument("--live", default=os.path.join(ROOT, "live_devices.json"))
    ap.add_argument("--dry-run", action="store_true", help="count events per stage and exit")
    ap.add_argument("--stage-timeout", type=float, default=600)
    args = ap.parse_args()

    out = stack.outputs(args.stack, args.region)
    config.LEDGER_TABLE = out["LedgerTable"]
    tbl = ledger.table(boto3.resource("dynamodb", region_name=args.region))
    meta = ledger.get_meta(tbl, args.notice)
    if meta is None:
        sys.exit("notice not seeded; run seed/seed.py first")
    window = float(meta.get("window_seconds", config.WINDOW_SECONDS))

    recipients = corpus.build_recipients(args.count, seed_mod.load_live(args.live))
    replayed = [r for r in recipients if not r["live_device"]]
    plan = corpus.plan_outcomes(replayed)
    events = corpus.build_corpus(args.notice, replayed, plan, datetime.now(timezone.utc), window)
    per_stage = {s: sum(1 for e in events if e["stage"] == s) for s in (1, 2, 3)}
    print(f"corpus: {len(events)} events {per_stage}, planned outcomes {corpus.expected_counts(plan)}")
    if args.dry_run:
        return

    sns = boto3.client("sns", region_name=args.region)
    topics = {k: out[v] for k, v in TOPIC_KEYS.items()}
    pool = ThreadPoolExecutor(max_workers=16)
    for stage in (1, 2, 3):
        if not wait_for_stage(tbl, args.notice, stage, args.stage_timeout):
            sys.exit(f"stage {stage} did not finish within {args.stage_timeout}s; is the notice started?")
        t0 = time.time()
        batch = [e for e in events if e["stage"] == stage]
        print(f"stage {stage}: releasing {len(batch)} events over {window:.0f}s")
        futures = []
        for e in batch:
            delay = t0 + e["frac"] * window - time.time()
            if delay > 0:
                time.sleep(delay)
            futures.append(pool.submit(sns.publish, TopicArn=topics[e["topic"]], Message=json.dumps(e["message"])))
        for f in futures:
            f.result()  # surface any publish failure instead of dropping events silently
    print("replay complete")


if __name__ == "__main__":
    main()
