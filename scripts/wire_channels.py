"""Point the three CDS services at the stack's event topics. Run after the identities exist.

    python scripts/wire_channels.py --rcs-agent rcs-xxxxxxxx
    python scripts/wire_channels.py --waba <WABA id or arn>

RCS: enables two-way messaging on the agent so suggestion taps reach the two-way topic.
     The configuration set with the delivery and read destination is created by the stack and
     is selected per message (ConfigurationSetName), so nothing else is needed.
WhatsApp: sets the stack's SNS topic as the event destination of the WhatsApp Business Account.
SES: needs no wiring; the stack's configuration set publishes to its topic.
"""
import argparse
import os
import sys

import boto3

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from seed import stack  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stack", default="proof-of-reach")
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    ap.add_argument("--rcs-agent", help="RCS agent id (rcs-...)")
    ap.add_argument("--waba", help="WhatsApp Business Account id or ARN as listed by list-linked-whatsapp-business-accounts")
    args = ap.parse_args()
    if not (args.rcs_agent or args.waba):
        ap.error("pass --rcs-agent and/or --waba")

    out = stack.outputs(args.stack, args.region)
    if args.rcs_agent:
        sms = boto3.client("pinpoint-sms-voice-v2", region_name=args.region)
        sms.update_rcs_agent(
            RcsAgentId=args.rcs_agent,
            TwoWayEnabled=True,
            TwoWayChannelArn=out["RcsTwoWayTopicArn"],
            TwoWayChannelRole=out["RcsTwoWayRoleArn"],
        )
        print(f"RCS agent {args.rcs_agent}: two-way messaging now publishes to {out['RcsTwoWayTopicArn']}")
    if args.waba:
        social = boto3.client("socialmessaging", region_name=args.region)
        social.put_whatsapp_business_account_event_destinations(
            id=args.waba, eventDestinations=[{"eventDestinationArn": out["WhatsAppTopicArn"]}]
        )
        print(f"WhatsApp account {args.waba}: events now publish to {out['WhatsAppTopicArn']}")


if __name__ == "__main__":
    main()
