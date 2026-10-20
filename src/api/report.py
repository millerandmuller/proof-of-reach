"""Aggregations for the reach bar (F11) and the reach report (F12). Pure functions over ledger items.

Every number is computed from recipient items only; nothing is estimated.
Tiers are exclusive buckets that always add up to N, so the bar and the report cannot disagree.
"""
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from common import config
from common.tiers import rank

BUCKETS = ("acknowledged", "read", "delivered", "sent", "unreachable", "not_started")
BUCKET_LABEL = {
    "acknowledged": "acknowledged", "read": "read only", "delivered": "delivered only",
    "sent": "sent only", "unreachable": "unreachable", "not_started": "not started",
}


WAITING_REASON = {
    "read": "read receipt only, not acknowledged",
    "delivered": "delivered, not acknowledged",
    "sent": "sent, no delivery event yet",
    "not_started": "not started",
}


def bucket(item: Dict[str, Any]) -> str:
    state = item["state"]
    return "not_started" if state == "none" else state


def tier_counts(items: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = Counter(bucket(i) for i in items)
    return {b: counts.get(b, 0) for b in BUCKETS}


def channel_breakdown(items: List[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    """Per channel: how many were sent on it and how far each got on that channel alone."""
    out = {ch: {"sent": 0, "delivered": 0, "read": 0, "acknowledged": 0} for ch in config.LADDER}
    for item in items:
        for ch, best in (item.get("by_channel") or {}).items():
            if ch not in out:
                continue
            r = rank(best)
            out[ch]["sent"] += 1 if r >= 1 else 0
            out[ch]["delivered"] += 1 if r >= 2 else 0
            out[ch]["read"] += 1 if r >= 3 else 0
            out[ch]["acknowledged"] += 1 if r >= 4 else 0
    return out


def percent(part: int, whole: int) -> int:
    return round(100 * part / whole) if whole else 0


def bar_label(counts: Dict[str, int], total: int) -> str:
    sent = total - counts["not_started"]
    return f"{percent(counts['acknowledged'], total)} % acknowledged · {percent(sent, total)} % sent"


def footer(items: List[Dict[str, Any]], meta: Dict[str, Any]) -> str:
    total = len(items)
    live = sum(1 for i in items if i.get("live_device"))
    source = "recorded events" if meta.get("replay_provenance") == "recorded" else "simulated events"
    return f"{total:,} recipients · {live} live devices · {total - live:,} replayed from {source}"


def human_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    if seconds >= 3600:
        return f"{seconds // 3600} h {seconds % 3600 // 60:02d} min"
    if seconds >= 60:
        return f"{seconds // 60} min {seconds % 60:02d} s"
    return f"{seconds} s"


def _parse(ts: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None
    except ValueError:
        return None


def elapsed_seconds(meta: Dict[str, Any], now: Optional[datetime] = None) -> float:
    start = _parse(meta.get("started_at"))
    if start is None:
        return 0.0
    end = _parse(meta.get("closed_at")) or now or datetime.now(timezone.utc)
    return (end - start).total_seconds()


def exception_rows(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Everyone not acknowledged, with the reason and the raw event ids behind it."""
    rows = []
    for item in items:
        if item["state"] == "acknowledged":
            continue
        history = item.get("tier_history", [])
        rows.append({
            "recipient_id": item["recipient_id"],
            "segment": item.get("segment"),
            "state": bucket(item),
            "reason": item.get("unreachable_reason") or WAITING_REASON[bucket(item)],
            "event_ids": [h["event_id"] for h in history if h.get("event_id")][-3:],
        })
    order = {"unreachable": 0, "read": 1, "delivered": 2, "sent": 3, "not_started": 4}
    rows.sort(key=lambda r: (order.get(r["state"], 9), r["recipient_id"]))
    return rows


def reason_groups(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    counts = Counter(r["reason"] for r in rows if r["state"] == "unreachable")
    return [{"reason": reason, "count": n} for reason, n in counts.most_common()]


def build_report(items: List[Dict[str, Any]], meta: Dict[str, Any], themes: List[Dict[str, Any]],
                 unmatched: int = 0, now: Optional[datetime] = None) -> Dict[str, Any]:
    total = len(items)
    counts = tier_counts(items)
    assert sum(counts.values()) == total, "tier buckets must add up to N"
    rows = exception_rows(items)
    closed = meta.get("status") == "closed"
    after = human_duration(elapsed_seconds(meta, now))
    headline = f"acknowledged {counts['acknowledged']:,} of {total:,} after {after}"
    breakdown = " · ".join(f"{BUCKET_LABEL[b]} {counts[b]:,}" for b in ("read", "delivered", "sent", "unreachable") if counts[b])
    k = counts["unreachable"] if closed else len(rows)
    last_line = (f"{k:,} people: unreachable, letter needed" if closed
                 else f"{k:,} people have not acknowledged yet")
    return {
        "headline": headline,
        "breakdown": breakdown,
        "last_line": last_line,
        "closed": closed,
        "counts": counts,
        "total": total,
        "channels": channel_breakdown(items),
        "reasons": reason_groups(rows),
        "exceptions": rows,
        "themes": themes,
        "unmatched_events": unmatched,
        "footer": footer(items, meta),
    }
