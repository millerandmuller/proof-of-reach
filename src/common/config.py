"""Runtime configuration, read from the environment in one place.

Every Lambda imports from here instead of calling os.environ directly, so the
README can list the full configuration surface and tests can override it.
"""
import os

REGION = os.environ.get("AWS_REGION", "us-east-1")
LEDGER_TABLE = os.environ.get("LEDGER_TABLE", "proof-of-reach-ledger")

# Channel ladder. Order is the escalation order; a recipient only climbs the
# rungs for channels listed in their capabilities.
LADDER = ("rcs", "whatsapp", "email")

# Demo compressor: length of one escalation window in seconds.
WINDOW_SECONDS = int(os.environ.get("WINDOW_SECONDS", "90"))

# CDS identities (set by the stack parameters; empty until the human unlocks them).
RCS_AGENT_ID = os.environ.get("RCS_AGENT_ID", "")
RCS_CONFIGURATION_SET = os.environ.get("RCS_CONFIGURATION_SET", "")
WHATSAPP_PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_TEMPLATE_NAME = os.environ.get("WHATSAPP_TEMPLATE_NAME", "")
SES_SENDER = os.environ.get("SES_SENDER", "")
SES_CONFIGURATION_SET = os.environ.get("SES_CONFIGURATION_SET", "")
ACK_BASE_URL = os.environ.get("ACK_BASE_URL", "")

# Notice writer: AgentCore Runtime ARN. Empty means "use stored default texts".
AGENT_RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN", "")

SCHEDULER_ROLE_ARN = os.environ.get("SCHEDULER_ROLE_ARN", "")
ENGINE_FUNCTION_ARN = os.environ.get("ENGINE_FUNCTION_ARN", "")

# Postback string shared by RCS suggestion, WhatsApp button id and email link.
POSTBACK_PREFIX = "por:v1"
