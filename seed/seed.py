"""Seed (or reset) the demo notice: 2,000 recipients, three of them live devices.

    python seed/seed.py --stack proof-of-reach            # create
    python seed/seed.py --stack proof-of-reach --reset    # wipe the notice, then create again

Live devices come from seed/live_devices.json (copy the .example file). Without it
every recipient is replayed and the footer says "0 live devices".
"""
import argparse
import json
import os
import sys

import boto3

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "..", "src"))
sys.path.insert(0, os.path.join(ROOT, ".."))

from common import config, ledger  # noqa: E402
from seed import corpus, stack  # noqa: E402

NOTICE_ID = "HX200-2026-10"
BRIEF = {
    "product": "heater HX-200", "hazard": "can overheat and cause a fire", "hazard_key": "overheat",
    "action": "unplug it and stop using it", "action_key": "unplug", "contact": "0800 555 0100",
}


def load_live(path: str):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return json.load(f)


def reset_notice(tbl, notice_id: str, recipient_ids) -> int:
    """Delete every item of the notice plus the replay message map entries. Returns items deleted."""
    keys = []
    kwargs = {"KeyConditionExpression": "pk = :pk", "ExpressionAttributeValues": {":pk": ledger.notice_pk(notice_id)},
              "ProjectionExpression": "pk, sk"}
    while True:
        resp = tbl.query(**kwargs)
        keys.extend((i["pk"], i["sk"]) for i in resp["Items"])
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    keys.extend((f"msg#replay-{ch}-{rid}", "map") for rid in recipient_ids for ch in corpus.LADDER)
    with tbl.batch_writer() as batch:
        for pk, sk in keys:
            batch.delete_item(Key={"pk": pk, "sk": sk})
    return len(keys)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stack", default="proof-of-reach")
    ap.add_argument("--region", default=config.REGION)
    ap.add_argument("--notice", default=NOTICE_ID)
    ap.add_argument("--count", type=int, default=2000)
    ap.add_argument("--window", type=int, default=config.WINDOW_SECONDS, help="seconds per escalation window")
    ap.add_argument("--live", default=os.path.join(ROOT, "live_devices.json"))
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()

    out = stack.outputs(args.stack, args.region)
    config.LEDGER_TABLE = out["LedgerTable"]
    tbl = ledger.table(boto3.resource("dynamodb", region_name=args.region))
    live = load_live(args.live)
    recipients = corpus.build_recipients(args.count, live)
    if args.reset:
        print(f"reset: deleted {reset_notice(tbl, args.notice, [r['recipient_id'] for r in recipients])} items")
    elif ledger.get_meta(tbl, args.notice):
        sys.exit("notice exists; pass --reset to wipe and re-seed")

    for r in recipients:
        ledger.put_recipient(tbl, args.notice, r)
    ack_base = out.get("DashboardUrl", "").rstrip("/")
    ledger.put_meta(tbl, args.notice, {
        "status": "ready", "title": "Recall: heater model HX-200, overheating", "brief": BRIEF,
        "ack_label": "Read, device off", "notice_url": f"{ack_base}/notice.html" if ack_base else "",
        "window_seconds": args.window, "live_ids": [d["recipient_id"] for d in live],
        "live_labels": {d["recipient_id"]: d.get("label", d["recipient_id"]) for d in live},
        "replay_provenance": corpus.PROVENANCE, "stage": 0,
    })
    print(f"seeded {len(recipients)} recipients ({len(live)} live, {len(recipients) - len(live)} replayed, "
          f"events {corpus.PROVENANCE}) for notice {args.notice}")


if __name__ == "__main__":
    main()
