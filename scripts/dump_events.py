"""Print the raw provider events stored in the ledger. Used for the WhatsApp button_reply spike
and for checking that every row of the dashboard has an AWS event behind it.

    python scripts/dump_events.py --channel whatsapp --last 5
    python scripts/dump_events.py --unmatched
"""
import argparse
import json
import os
import sys

import boto3

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, ROOT)
from common import config, ledger  # noqa: E402
from seed import stack  # noqa: E402


def decode_raw(raw_json: str) -> dict:
    """The stored raw event; a WhatsApp entry arrives as a JSON string inside it and is decoded for reading."""
    raw = json.loads(raw_json)
    if isinstance(raw, dict) and isinstance(raw.get("whatsAppWebhookEntry"), str):
        raw["whatsAppWebhookEntry"] = json.loads(raw["whatsAppWebhookEntry"])
    return raw


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stack", default="proof-of-reach")
    ap.add_argument("--region", default=config.REGION)
    ap.add_argument("--notice", default="HX200-2026-10")
    ap.add_argument("--channel", choices=config.LADDER)
    ap.add_argument("--last", type=int, default=5)
    ap.add_argument("--unmatched", action="store_true", help="show events that could not be tied to a recipient")
    ap.add_argument("--include-replayed", action="store_true")
    args = ap.parse_args()

    config.LEDGER_TABLE = stack.outputs(args.stack, args.region)["LedgerTable"]
    tbl = ledger.table(boto3.resource("dynamodb", region_name=args.region))
    if args.unmatched:
        # events that carried no notice id are filed under "unknown"
        items = ledger.query_prefix(tbl, args.notice, "unmatched#") + ledger.query_prefix(tbl, "unknown", "unmatched#")
    else:
        items = ledger.query_prefix(tbl, args.notice, "event#")
    if args.channel:
        items = [i for i in items if i.get("channel") == args.channel]
    if not args.include_replayed:
        items = [i for i in items if not i.get("replayed")]
    items.sort(key=lambda i: str(i.get("provider_timestamp") or i.get("at") or ""))
    for item in items[-args.last:]:
        print(f"--- {item.get('channel')} {item.get('event')} tier={item.get('tier')} message={item.get('provider_message_id')} at={item.get('provider_timestamp') or item.get('at')}")
        print(json.dumps(decode_raw(item["raw_json"]), indent=2))
    if not items:
        print("no matching events")


if __name__ == "__main__":
    main()
