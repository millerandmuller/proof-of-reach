"""F9: email stage. SES v2 SendEmail with a configuration set; the link click is the acknowledgment (S-23)."""
from typing import Dict
from urllib.parse import quote

from common import config


def ack_link(postback: str) -> str:
    # Colons stay literal: the click event echoes the link and tiers.parse_postback reads it back.
    return f"{config.ACK_BASE_URL.rstrip('/')}/ack.html?p={quote(postback, safe=':')}"


def build_body(texts: Dict[str, str], postback: str) -> Dict[str, Dict[str, str]]:
    link = ack_link(postback)
    text = f"{texts['body']}\n\n{texts['ack_label']}: {link}\n"
    html = (
        f"<p>{texts['body']}</p>"
        f"<p><a href=\"{link}\">{texts['ack_label']}</a></p>"
    )
    return {"Text": {"Data": text}, "Html": {"Data": html}}


def send(client, address: str, texts: Dict[str, str], postback: str) -> str:
    resp = client.send_email(  # CDS call: Amazon SES v2 (F9)
        FromEmailAddress=config.SES_SENDER,
        Destination={"ToAddresses": [address]},
        ConfigurationSetName=config.SES_CONFIGURATION_SET,
        Content={"Simple": {"Subject": {"Data": texts["subject"]}, "Body": build_body(texts, postback)}},
    )
    return resp["MessageId"]
