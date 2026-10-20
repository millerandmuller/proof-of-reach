"""F4: WhatsApp event destination (SNS -> SQS) -> ledger. Statuses, button replies, free text."""
from common import normalize, pipeline
from consumers import batch


def handle(tbl, message, _sns_timestamp) -> int:
    envelopes = normalize.normalize_whatsapp(message)
    for env in envelopes:
        pipeline.process(tbl, env)
    return len(envelopes)


handler = batch.make_handler(handle)
