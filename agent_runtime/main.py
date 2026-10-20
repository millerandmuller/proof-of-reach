"""AgentCore Runtime entrypoint: the notice writer and the reply summarizer (F14, F15).

Implements the HTTP contract directly with the standard library (port 8080,
GET /ping, POST /invocations) so the zip has no native dependencies.
The agent only writes text. Escalation, windows and the ledger are code elsewhere.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

import boto3

from notice_text import build_prompt, parse_model_output

MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
REGION = os.environ.get("AWS_REGION", "us-east-1")
_bedrock = None


def bedrock():
    global _bedrock
    if _bedrock is None:
        _bedrock = boto3.client("bedrock-runtime", region_name=REGION)
    return _bedrock


def converse(prompt: str, max_tokens: int = 1800) -> str:
    resp = bedrock().converse(
        modelId=MODEL_ID,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": max_tokens, "temperature": 0.2},
    )
    return resp["output"]["message"]["content"][0]["text"]


def write_notice(payload: dict) -> dict:
    return {"texts": parse_model_output(converse(build_prompt(payload["brief"]))), "model_id": MODEL_ID}


def summarize_replies(payload: dict) -> dict:
    replies = payload.get("replies", [])
    if not replies:
        return {"themes": [], "model_id": MODEL_ID}
    numbered = "\n".join(f"{i + 1}. {r}" for i, r in enumerate(replies))
    prompt = (
        "Group these free-text replies to a safety notice into themes. For each theme give a short label (max 8 words) "
        "and the list of reply numbers in it. Every reply belongs to exactly one theme. "
        'Return only JSON: {"themes": [{"label": "...", "replies": [1, 2]}]}\n' + numbered
    )
    out = parse_model_output(converse(prompt, max_tokens=800))
    return {"themes": out.get("themes", []), "model_id": MODEL_ID}


ACTIONS = {"write_notice": write_notice, "summarize_replies": summarize_replies}


def invoke(payload: dict) -> dict:
    action = payload.get("action")
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    return ACTIONS[action](payload)


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/ping":
            self._send(200, {"status": "Healthy"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/invocations":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            self._send(200, invoke(json.loads(self.rfile.read(length) or b"{}")))
        except ValueError as err:  # bad input or unparsable model output: caller falls back to stored text
            self._send(400, {"error": str(err)})

    def log_message(self, fmt, *args):  # CloudWatch already captures stdout; keep it quiet
        pass


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
