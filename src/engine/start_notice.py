"""Start a seeded notice: claim it once, have the agent write the texts, hand stage 1 to the engine.

The start is one-shot per notice (the claim is conditional), so a public start URL
cannot be used to send the same notice twice; `seed/seed.py --reset` re-arms it.
"""
import json
from typing import Any, Callable, Dict

import boto3

from agent import client as agent_client
from common import config, ledger


def invoke_engine_async(notice_id: str, stage: int, lambda_client=None) -> None:
    lambda_client = lambda_client or boto3.client("lambda", region_name=config.REGION)
    lambda_client.invoke(
        FunctionName=config.ENGINE_FUNCTION_ARN,
        InvocationType="Event",
        Payload=json.dumps({"notice_id": notice_id, "stage": stage}).encode(),
    )


def start(tbl, notice_id: str, writer: Callable = agent_client.write_notice,
          invoke_engine: Callable = invoke_engine_async) -> Dict[str, Any]:
    meta = ledger.get_meta(tbl, notice_id)
    if meta is None:
        return {"ok": False, "error": "unknown notice", "status": 404}
    if not ledger.claim_start(tbl, notice_id):
        return {"ok": False, "error": "notice already started", "status": 409}
    texts, sources, error = writer(meta["brief"])
    started = {**meta, "status": "running", "texts": texts, "text_sources": sources, "text_error": error,
               "started_at": ledger.now_iso()}
    ledger.put_meta(tbl, notice_id, started)
    invoke_engine(notice_id, 1)
    return {"ok": True, "status": 202, "notice_id": notice_id, "text_sources": sources, "text_error": error}
