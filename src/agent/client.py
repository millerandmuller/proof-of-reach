"""Lambda-side client for the notice writer on AgentCore Runtime, with stored-text fallback.

The ladder never waits for the model: any failure here returns default texts,
and the notice meta records which texts were written by the agent.
"""
import json
import logging
import uuid
from typing import Any, Dict, Optional, Tuple

import boto3
from botocore.config import Config

from agent import notice_text
from common import config

log = logging.getLogger(__name__)


# The model call must finish well inside the API Lambda's 60 s: fail fast and use the stored texts instead.
RUNTIME_CONFIG = Config(read_timeout=25, connect_timeout=5, retries={"max_attempts": 1})


def invoke_runtime(payload: Dict[str, Any], client=None) -> Dict[str, Any]:
    client = client or boto3.client("bedrock-agentcore", region_name=config.REGION, config=RUNTIME_CONFIG)
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=config.AGENT_RUNTIME_ARN,
        runtimeSessionId=f"por-{uuid.uuid4().hex}",  # session ids must be at least 33 characters
        payload=json.dumps(payload).encode(),
    )
    return json.loads(resp["response"].read())


def write_notice(brief: Dict[str, str], client=None) -> Tuple[Dict[str, Any], Dict[str, str], Optional[str]]:
    """Returns (texts, sources, error). sources maps 'segment.channel' to 'agent' or 'default'."""
    if not config.AGENT_RUNTIME_ARN and client is None:
        return (*notice_text.merge_with_defaults({}, brief), "no AGENT_RUNTIME_ARN configured")
    try:
        reply = invoke_runtime({"action": "write_notice", "brief": brief}, client)
        texts, sources = notice_text.merge_with_defaults(reply["texts"], brief)
        return texts, sources, None
    except Exception as err:  # noqa: BLE001 - any failure of the optional writer must fall back, never block the notice
        log.exception("notice writer failed, using stored texts")
        texts, sources = notice_text.merge_with_defaults({}, brief)
        return texts, sources, f"{type(err).__name__}: writer unavailable"


def summarize_replies(replies, client=None) -> Dict[str, Any]:
    """Theme lines for the report. On failure returns an empty list and the reason."""
    if not replies:
        return {"themes": [], "error": None}
    try:
        out = invoke_runtime({"action": "summarize_replies", "replies": replies}, client)
        return {"themes": notice_text.clean_themes(out.get("themes", []), len(replies)), "error": None}
    except Exception as err:  # noqa: BLE001 - the report must render without the summary
        log.exception("reply summarizer failed")
        return {"themes": [], "error": f"{type(err).__name__}: summarizer unavailable"}
