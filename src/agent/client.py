"""Lambda-side client for the notice writer on AgentCore Runtime, with stored-text fallback.

The ladder never waits for the model: any failure here returns default texts,
and the notice meta records which texts were written by the agent (F14).
"""
import json
import logging
import uuid
from typing import Any, Dict, Optional, Tuple

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from agent import notice_text
from common import config

log = logging.getLogger(__name__)


def invoke_runtime(payload: Dict[str, Any], client=None) -> Dict[str, Any]:
    client = client or boto3.client("bedrock-agentcore", region_name=config.REGION)
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
    except (ClientError, BotoCoreError, ValueError, KeyError) as err:
        log.warning("notice writer unavailable, using stored texts: %s", err)
        texts, sources = notice_text.merge_with_defaults({}, brief)
        return texts, sources, str(err)


def summarize_replies(replies, client=None) -> Dict[str, Any]:
    """Theme lines for the report. On failure returns an empty list and the reason."""
    if not replies:
        return {"themes": [], "error": None}
    try:
        out = invoke_runtime({"action": "summarize_replies", "replies": replies}, client)
        return {"themes": out.get("themes", []), "error": None}
    except (ClientError, BotoCoreError, ValueError, KeyError) as err:
        log.warning("reply summarizer unavailable: %s", err)
        return {"themes": [], "error": str(err)}
