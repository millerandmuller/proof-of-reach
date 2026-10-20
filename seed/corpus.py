"""Deterministic demo population and replay corpus (F13). Pure: no AWS, no clock reads.

Each replayed recipient gets a planned outcome. The corpus holds provider events in
the same payload shapes the real services publish (S-03, S-04, S-16, S-23), so the
real consumers process them. Provenance is "simulated" until payload templates are
captured from the live pipeline; the dashboard footer reads that provenance and
never calls simulated events "recorded".
"""
import json
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from common.tiers import build_postback

PROVENANCE = "simulated"
# Outcome shares of the replayed population, from the demo script (1,643 / 212 / 98 / 47 of 2,000).
SHARES = (("acknowledged", 0.8215), ("read", 0.106), ("delivered", 0.049), ("unreachable", 0.0235))
ACK_RUNG_WEIGHTS = (0.62, 0.24, 0.14)  # share of acknowledgments that happen on rung 1, 2, 3
CAP_RATES = {"rcs": 0.60, "whatsapp": 0.70, "email": 0.95}
LADDER = ("rcs", "whatsapp", "email")


def build_recipients(n: int, live: List[Dict[str, Any]], seed: int = 20261026) -> List[Dict[str, Any]]:
    """n recipients: the live devices first (from live_devices.json), the rest synthetic."""
    rng = random.Random(seed)
    out = [dict(d, live_device=True) for d in live]
    for i in range(len(out), n):
        rid = f"r-{i:06d}"
        out.append({
            "recipient_id": rid, "segment": "dealer" if rng.random() < 0.08 else "consumer",
            "phone": f"+1555{rng.randint(1000000, 9999999)}", "email": f"{rid}@example.test" if rng.random() < CAP_RATES["email"] else "",
            "rcs_capable": rng.random() < CAP_RATES["rcs"], "whatsapp": rng.random() < CAP_RATES["whatsapp"],
            "live_device": False,
        })
    return out


def caps_of(r: Dict[str, Any]) -> List[str]:
    have = {"rcs": r.get("rcs_capable"), "whatsapp": r.get("whatsapp"), "email": bool(r.get("email"))}
    return [c for c in LADDER if have[c]]


def plan_outcomes(replayed: List[Dict[str, Any]], seed: int = 7) -> Dict[str, Dict[str, Any]]:
    """Outcome per replayed recipient: {rid: {"outcome", "rung", "silent"}}. Quotas are exact."""
    rng = random.Random(seed)
    plan: Dict[str, Dict[str, Any]] = {}
    pool = []
    for r in replayed:
        if not caps_of(r):
            plan[r["recipient_id"]] = {"outcome": "unreachable", "rung": 0, "silent": True}  # engine verdict, no events
        else:
            pool.append(r["recipient_id"])
    rng.shuffle(pool)
    total = len(replayed)
    quotas = {name: round(total * share) for name, share in SHARES}
    quotas["unreachable"] = max(quotas["unreachable"] - sum(1 for p in plan.values() if p["outcome"] == "unreachable"), 0)
    quotas["acknowledged"] = len(pool) - quotas["read"] - quotas["delivered"] - quotas["unreachable"]
    by_id = {r["recipient_id"]: r for r in replayed}
    cursor = 0
    for name in ("acknowledged", "read", "delivered", "unreachable"):
        for rid in pool[cursor: cursor + quotas[name]]:
            m = len(caps_of(by_id[rid]))
            rung = m
            if name == "acknowledged":
                weights = ACK_RUNG_WEIGHTS[:m]
                rung = rng.choices(range(1, m + 1), weights=weights)[0]
            plan[rid] = {"outcome": name, "rung": rung, "silent": name == "unreachable" and rng.random() < 0.5}
        cursor += quotas[name]
    return plan


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _rcs_status(event: str, status: str, mid: str, at: datetime, desc: str = "") -> Dict[str, Any]:
    return {"eventType": event, "eventVersion": "1.0", "eventTimestamp": int(at.timestamp() * 1000), "isFinal": True,
            "messageId": mid, "messageStatus": status, "messageStatusDescription": desc,
            "rcsBusinessId": "replay_agent", "agentIsoCountryCode": "US"}


def _whatsapp(entry_value: Dict[str, Any], at: datetime) -> Dict[str, Any]:
    entry = {"id": "replay-waba", "changes": [{"field": "messages", "value": {"messaging_product": "whatsapp", **entry_value}}]}
    return {"context": {"MetaWabaIds": [], "MetaPhoneNumberIds": []}, "whatsAppWebhookEntry": json.dumps(entry),
            "aws_account_id": "000000000000", "message_timestamp": _iso(at), "messageId": f"replay-aws-{int(at.timestamp() * 1000)}"}


def _ses(event: str, mid: str, at: datetime, **extra) -> Dict[str, Any]:
    return {"eventType": event, "mail": {"messageId": mid, "timestamp": _iso(at)}, **extra}


def _events_for_rung(rid: str, notice_id: str, channel: str, steps: str, at0: datetime, window: float, rng: random.Random):
    """Events for one rung. steps: 'silent' | 'fail' | 'delivered' | 'read' | 'ack'. Yields (frac, topic, message)."""
    mid = f"replay-{channel}-{rid}"
    pb = build_postback(notice_id, rid)
    at = lambda frac: at0 + timedelta(seconds=frac * window)
    f_del = rng.uniform(0.04, 0.12)
    f_read = f_del + rng.uniform(0.08, 0.22)
    f_ack = f_read + rng.uniform(0.04, 0.16)  # latest possible: 0.5 of the window, the rest is slack for queueing
    if steps == "silent":
        return
    if channel == "rcs":
        if steps == "fail":
            yield f_del, "rcs_events", _rcs_status("RCS_DELIVERED", "UNDELIVERABLE", mid, at(f_del), "Device is not RCS capable")
            return
        yield f_del, "rcs_events", _rcs_status("RCS_DELIVERED", "DELIVERED", mid, at(f_del), "Message has been accepted by phone")
        if steps in ("read", "ack"):
            yield f_read, "rcs_events", _rcs_status("RCS_READ", "READ", mid, at(f_read), "Message has been read by recipient")
        if steps == "ack":
            body = json.dumps({"type": "SUGGESTION", "text": "Device off, read", "postbackData": pb})
            yield f_ack, "rcs_twoway", {"originationNumber": "15550000000", "destinationNumber": "replay_agent", "messageBody": body,
                                       "inboundMessageId": f"replay-in-{rid}", "previousPublishedMessageId": mid}
    elif channel == "whatsapp":
        if steps == "fail":
            yield f_del, "whatsapp", _whatsapp({"statuses": [{"id": mid, "status": "failed", "timestamp": str(int(at(f_del).timestamp())),
                                                              "recipient_id": "15550000000",
                                                              "errors": [{"code": 131026, "title": "Message undeliverable"}]}]}, at(f_del))
            return
        yield f_del, "whatsapp", _whatsapp({"statuses": [{"id": mid, "status": "delivered", "timestamp": str(int(at(f_del).timestamp())), "recipient_id": "15550000000"}]}, at(f_del))
        if steps in ("read", "ack"):
            yield f_read, "whatsapp", _whatsapp({"statuses": [{"id": mid, "status": "read", "timestamp": str(int(at(f_read).timestamp())), "recipient_id": "15550000000"}]}, at(f_read))
        if steps == "ack":
            msg = {"from": "15550000000", "id": f"replay-in-{rid}", "timestamp": str(int(at(f_ack).timestamp())), "type": "interactive",
                   "context": {"id": mid}, "interactive": {"type": "button_reply", "button_reply": {"id": pb, "title": "Device off, read"}}}
            yield f_ack, "whatsapp", _whatsapp({"messages": [msg]}, at(f_ack))
    else:
        if steps == "fail":
            yield f_del, "ses", _ses("Bounce", mid, at(f_del), bounce={"bounceType": "Permanent", "bounceSubType": "NoEmail", "timestamp": _iso(at(f_del))})
            return
        yield f_del, "ses", _ses("Delivery", mid, at(f_del), delivery={"timestamp": _iso(at(f_del))})
        if steps in ("read", "ack"):
            yield f_read, "ses", _ses("Open", mid, at(f_read), open={"timestamp": _iso(at(f_read)), "isBotEvent": "Unlikely"})
        if steps == "ack":
            link = f"https://replay.example/ack.html?p={pb}"
            yield f_ack, "ses", _ses("Click", mid, at(f_ack), click={"timestamp": _iso(at(f_ack)), "link": link, "isBotEvent": "Unlikely"})


def build_corpus(notice_id: str, replayed: List[Dict[str, Any]], plan: Dict[str, Dict[str, Any]], base: datetime,
                 window: float, seed: int = 11) -> List[Dict[str, Any]]:
    """All replay events, each {stage, frac, topic, message}, sorted by (stage, frac)."""
    rng = random.Random(seed)
    events: List[Dict[str, Any]] = []
    for r in replayed:
        p = plan[r["recipient_id"]]
        caps = caps_of(r)
        if not caps or p["silent"]:
            continue
        for rung, channel in enumerate(caps, start=1):
            if p["outcome"] == "acknowledged":
                steps = "ack" if rung == p["rung"] else ("delivered" if rung < p["rung"] else None)
            elif p["outcome"] == "read":
                steps = "read" if rung == p["rung"] else "delivered"
            elif p["outcome"] == "delivered":
                steps = "delivered"
            else:  # unreachable with provider failures
                steps = "fail"
            if steps is None:
                continue  # the engine never sends this rung: the recipient acknowledged earlier
            at0 = base + timedelta(seconds=(rung - 1) * window)
            for frac, topic, message in _events_for_rung(r["recipient_id"], notice_id, channel, steps, at0, window, rng):
                events.append({"stage": rung, "frac": round(frac, 4), "topic": topic, "message": message})
    events.sort(key=lambda e: (e["stage"], e["frac"]))
    return events


def expected_counts(plan: Dict[str, Dict[str, Any]]) -> Dict[str, int]:
    counts = {"acknowledged": 0, "read": 0, "delivered": 0, "unreachable": 0}
    for p in plan.values():
        counts[p["outcome"]] += 1
    return counts
