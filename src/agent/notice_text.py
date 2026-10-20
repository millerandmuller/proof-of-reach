"""Notice text: prompt, output parsing, deterministic validator, stored defaults (F14).

Pure Python with no AWS imports, so the same file runs inside the AgentCore
Runtime package, inside the start Lambda (for validation and fallback) and in tests.
The model writes text; this module decides whether the text is acceptable.
"""
import json
import re
from typing import Any, Dict, List

SEGMENTS = ("consumer", "dealer")
CHANNELS = ("rcs", "whatsapp", "email")
MAX_BODY = 320  # one thumb length
BANNED_WORDS = re.compile(r"\b(free|exclusive|amazing|best|discount|offer)\b")

SCHEMA_HINT = (
    '{"consumer": {"rcs": {"title": "...", "body": "..."}, "whatsapp": {"body": "..."}, '
    '"email": {"subject": "...", "body": "..."}}, "dealer": {...same shape...}}'
)


def build_prompt(brief: Dict[str, str]) -> str:
    return (
        "You write mandatory safety notices. Write for two audiences, consumer and dealer, "
        "and for three channels: rcs (title and body), whatsapp (body), email (subject and body).\n"
        f"Every body must be at most {MAX_BODY} characters, plain, factual, no marketing words, no exclamation marks, "
        "and must contain these four elements in this order: the product name, the hazard, the action, the contact.\n"
        f"Product: {brief['product']}\nHazard: {brief['hazard']}\nAction: {brief['action']}\nContact: {brief['contact']}\n"
        f"The text must contain the product name, the word stem '{brief['hazard_key']}', the word stem '{brief['action_key']}' "
        f"and the contact exactly as written. The dealer version also tells the dealer to stop selling the product.\n"
        f"Return only JSON in this shape, nothing else: {SCHEMA_HINT}"
    )


def parse_model_output(text: str) -> Dict[str, Any]:
    """Extract the JSON object from a model reply, tolerating code fences and prose around it."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("model reply contains no JSON object")
    return json.loads(match.group(0))


def validate_body(body: str, brief: Dict[str, str]) -> List[str]:
    """Problems with one body text; an empty list means acceptable."""
    problems: List[str] = []
    low = body.lower()
    if len(body) > MAX_BODY:
        problems.append(f"longer than {MAX_BODY} characters")
    positions = []
    for label, needle in (("product", brief["product"]), ("hazard", brief["hazard_key"]),
                          ("action", brief["action_key"]), ("contact", brief["contact"])):
        idx = low.find(needle.lower())
        if idx < 0:
            problems.append(f"missing {label}")
        positions.append(idx)
    found = [p for p in positions if p >= 0]
    if len(found) == 4 and found != sorted(found):
        problems.append("elements out of order (product, hazard, action, contact)")
    if BANNED_WORDS.search(low) or "!" in body:
        problems.append("marketing wording")
    return problems


def _bodies(texts: Dict[str, Any]):
    for segment in SEGMENTS:
        for channel in CHANNELS:
            yield segment, channel, texts.get(segment, {}).get(channel, {}).get("body")


def validate_texts(texts: Dict[str, Any], brief: Dict[str, str]) -> Dict[str, List[str]]:
    """Problems keyed 'segment.channel'. Missing or non-string bodies count as problems."""
    out: Dict[str, List[str]] = {}
    for segment, channel, body in _bodies(texts):
        key = f"{segment}.{channel}"
        if not isinstance(body, str) or not body.strip():
            out[key] = ["missing body"]
            continue
        problems = validate_body(body, brief)
        if channel == "rcs" and not str(texts[segment][channel].get("title", "")).strip():
            problems.append("missing title")
        if channel == "email" and not str(texts[segment][channel].get("subject", "")).strip():
            problems.append("missing subject")
        if problems:
            out[key] = problems
    return out


def default_texts(brief: Dict[str, str]) -> Dict[str, Any]:
    """Stored fallback text. Satisfies the validator by construction; the UI marks it 'default text'."""
    core = f"{brief['product']}: {brief['hazard']}. Please {brief['action']}. Contact {brief['contact']}."
    dealer = f"{brief['product']}: {brief['hazard']}. Stop selling it and {brief['action']}. Contact {brief['contact']}."
    title = f"Safety notice: {brief['product']}"
    return {
        "consumer": {"rcs": {"title": title, "body": core}, "whatsapp": {"body": core},
                     "email": {"subject": title, "body": core}},
        "dealer": {"rcs": {"title": title, "body": dealer}, "whatsapp": {"body": dealer},
                   "email": {"subject": title, "body": dealer}},
    }


def merge_with_defaults(model_texts: Dict[str, Any], brief: Dict[str, str]):
    """Keep every model text that validates, replace the rest with defaults.

    Returns (texts, sources) where sources maps 'segment.channel' to 'agent' or 'default'.
    """
    defaults = default_texts(brief)
    problems = validate_texts(model_texts, brief) if isinstance(model_texts, dict) else {}
    texts: Dict[str, Any] = {}
    sources: Dict[str, str] = {}
    for segment in SEGMENTS:
        texts[segment] = {}
        for channel in CHANNELS:
            key = f"{segment}.{channel}"
            candidate = (model_texts or {}).get(segment, {}).get(channel) if isinstance(model_texts, dict) else None
            if candidate is not None and key not in problems and isinstance(candidate, dict):
                texts[segment][channel] = candidate
                sources[key] = "agent"
            else:
                texts[segment][channel] = defaults[segment][channel]
                sources[key] = "default"
    return texts, sources
