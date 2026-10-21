"""Proof tiers and the postback format. Pure functions, no AWS calls.

The tiers are never collapsed: sent < delivered < read < acknowledged.
Only `acknowledged` is proof; `read` is a receipt the recipient can switch off.
"""
from typing import Optional, Tuple
from urllib.parse import unquote

from common.config import POSTBACK_PREFIX

TIER_RANK = {"sent": 1, "delivered": 2, "read": 3, "acknowledged": 4}
UNREACHABLE = "unreachable"
UNPARSED = "unparsed"  # a tap or button whose postback is not one we issued: stored, never counted
URL_SUFFIX = ":url"  # the OpenUrl suggestion of the RCS card: a tap proves the card was opened, not acknowledged


def rank(state: str) -> int:
    """Rank of a state; unreachable and unknown states rank below `sent`."""
    return TIER_RANK.get(state, 0)


def build_postback(notice_id: str, recipient_id: str) -> str:
    return f"{POSTBACK_PREFIX}:{notice_id}:{recipient_id}:ack"


def e164(number) -> Optional[str]:
    """`+digits` form of a phone number however it was written ("+1 555 ...", "1555..."), None when it has no digits."""
    digits = "".join(ch for ch in str(number or "") if ch.isdigit())
    return f"+{digits}" if digits else None


def build_url_postback(postback: str) -> str:
    return postback + URL_SUFFIX


def parse_url_postback(text: str) -> Optional[Tuple[str, str]]:
    """(notice_id, recipient_id) when `text` is the postback of the OpenUrl suggestion, else None."""
    if not text or not text.endswith(URL_SUFFIX):
        return None
    return parse_postback(text[: -len(URL_SUFFIX)])


def parse_postback(text: str) -> Optional[Tuple[str, str]]:
    """Return (notice_id, recipient_id) from a postback string, else None.

    The string may be embedded in a longer value (a URL path, a button id), so
    we search for the prefix instead of requiring an exact match.
    """
    if not text:
        return None
    text = unquote(text)  # mail clients and link rewriters may percent-encode the colons
    start = text.find(POSTBACK_PREFIX + ":")
    if start < 0:
        return None
    # Stop at the first character that cannot be part of the token.
    token = text[start:].split("?")[0].split("&")[0].split("#")[0].split('"')[0].split("/")[0]
    parts = token.split(":")
    if len(parts) != 5 or parts[0] != "por" or parts[1] != "v1" or parts[4] != "ack":
        return None
    notice_id, recipient_id = parts[2], parts[3]
    if not notice_id or not recipient_id:
        return None
    return notice_id, recipient_id


def next_state(current: str, incoming: str) -> str:
    """State after an incoming tier. A tier never regresses.

    `unreachable` is a verdict of the rule engine, not a proof tier: a delivery, read or
    acknowledgment replaces it, because proof beats the verdict. A late `sent` does not:
    the provider accepting a message is not a delivery, and the verdict means "never delivered".
    """
    if current == UNREACHABLE:
        return incoming if rank(incoming) >= rank("delivered") else current
    return incoming if rank(incoming) > rank(current) else current
