"""The README names file and line of every CDS runtime call; those references must stay true."""
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..")
CALLS = {"SendRcsMessage": "send_rcs_message", "PutMessageFeedback": "put_message_feedback",
         "SendWhatsAppMessage": "send_whatsapp_message", "SendEmail": "send_email"}


def read(path):
    with open(os.path.join(ROOT, path)) as f:
        return f.read()


def test_every_readme_call_reference_points_at_the_real_call():
    readme = read("README.md")
    rows = re.findall(r"\| [^|]+ \| `(\w+)` \| `([\w/._]+):(\d+)` \|", readme)
    assert {r[0] for r in rows} == set(CALLS), "README must list all four CDS calls with file:line"
    for api, path, line in rows:
        lines = read(path).splitlines()
        assert CALLS[api] in lines[int(line) - 1], f"{path}:{line} is not the {api} call"


def test_readme_run_commands_exist():
    readme = read("README.md")
    for rel in re.findall(r"(scripts/[\w_.]+|seed/[\w_.]+\.py)", readme):
        if rel.endswith(".example.json"):
            continue
        assert os.path.exists(os.path.join(ROOT, rel)), f"README mentions missing file {rel}"
