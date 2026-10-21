"""RCS stage. SendRcsMessage rich card with one Reply suggestion and one OpenUrl."""
from typing import Any, Dict

from common import config
from common.tiers import build_url_postback


def build_content(texts: Dict[str, str], postback: str, notice_url: str) -> Dict[str, Any]:
    """Rich card: title, description, a Reply whose PostbackData is the acknowledgment, an OpenUrl to the notice."""
    suggestions = [{"Reply": {"Text": texts["ack_label"][:25], "PostbackData": postback}}]
    if notice_url:
        suggestions.append({"OpenUrl": {"Text": "Read the notice", "PostbackData": build_url_postback(postback), "Url": notice_url}})
    return {
        "Content": {
            "RichCard": {
                "CardOrientation": "VERTICAL",
                "CardContent": {
                    "Title": texts["title"][:200],
                    "Description": texts["body"][:2000],
                    "Suggestions": suggestions,
                },
            }
        }
    }


def send(client, phone: str, texts: Dict[str, str], postback: str, notice_url: str = "") -> str:
    """Returns the provider message id. MessageFeedbackEnabled lets the loop closer report the tap later."""
    resp = client.send_rcs_message(  # CDS call: End User Messaging RCS
        DestinationPhoneNumber=phone,
        OriginationIdentity=config.RCS_AGENT_ID,
        ConfigurationSetName=config.RCS_CONFIGURATION_SET,
        MessageFeedbackEnabled=True,
        RcsMessageContent=build_content(texts, postback, notice_url),
    )
    return resp["MessageId"]
