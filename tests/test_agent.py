import io
import json

from agent import client as agent_client
from agent import notice_text as nt
from common import config

BRIEF = {"product": "heater HX-200", "hazard": "can overheat and cause a fire", "hazard_key": "overheat",
         "action": "unplug it and stop using it", "action_key": "unplug", "contact": "0800 555 0100"}


def good(extra=""):
    return f"Heater HX-200 can overheat. Please unplug it and stop using it. Call 0800 555 0100.{extra}"


def test_default_texts_pass_the_validator():
    assert nt.validate_texts(nt.default_texts(BRIEF), BRIEF) == {}


def test_validator_rejects_missing_action_line():
    problems = nt.validate_body("Heater HX-200 can overheat. Call 0800 555 0100.", BRIEF)
    assert "missing action" in problems


def test_validator_rejects_order_length_and_marketing():
    assert any("out of order" in p for p in nt.validate_body("Call 0800 555 0100. Unplug heater HX-200, it can overheat.", BRIEF))
    assert any("longer than" in p for p in nt.validate_body(good(" x" * 200), BRIEF))
    assert "marketing wording" in nt.validate_body(good(" Best offer!"), BRIEF)


def test_parse_model_output_tolerates_fences():
    assert nt.parse_model_output('Here you go:\n```json\n{"a": 1}\n```') == {"a": 1}


def model_reply(consumer_rcs_body=None):
    d = nt.default_texts(BRIEF)
    if consumer_rcs_body is not None:
        d["consumer"]["rcs"]["body"] = consumer_rcs_body
    return d


def test_merge_keeps_valid_agent_text_and_replaces_invalid():
    texts, sources = nt.merge_with_defaults(model_reply("Just call us."), BRIEF)
    assert sources["consumer.rcs"] == "default" and sources["dealer.rcs"] == "agent"
    assert nt.validate_texts(texts, BRIEF) == {}


class FakeAgent:
    def __init__(self, reply=None, exc=None):
        self.reply, self.exc, self.calls = reply, exc, []

    def invoke_agent_runtime(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        return {"response": io.BytesIO(json.dumps(self.reply).encode())}


def test_client_uses_agent_texts_when_valid(monkeypatch):
    fake = FakeAgent({"texts": model_reply(), "model_id": "m"})
    texts, sources, err = agent_client.write_notice(BRIEF, client=fake)
    assert err is None and set(sources.values()) == {"agent"}
    assert len(fake.calls[0]["runtimeSessionId"]) >= 33


def test_client_falls_back_when_runtime_fails_or_returns_garbage(monkeypatch):
    from botocore.exceptions import ClientError
    err = ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "InvokeAgentRuntime")
    texts, sources, e = agent_client.write_notice(BRIEF, client=FakeAgent(exc=err))
    assert e and set(sources.values()) == {"default"} and nt.validate_texts(texts, BRIEF) == {}
    texts, sources, e = agent_client.write_notice(BRIEF, client=FakeAgent({"nope": 1}))
    assert e and set(sources.values()) == {"default"}


def test_client_without_runtime_arn_uses_defaults(monkeypatch):
    monkeypatch.setattr(config, "AGENT_RUNTIME_ARN", "")
    _, sources, e = agent_client.write_notice(BRIEF)
    assert set(sources.values()) == {"default"} and "AGENT_RUNTIME_ARN" in e
