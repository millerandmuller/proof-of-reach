"""F4: WhatsApp event destination (SNS) -> ledger. Statuses, button replies, free text."""
import json

from common import ledger, normalize, pipeline


def handler(event, _context=None):
    tbl = ledger.table()
    count = 0
    for record in event["Records"]:
        for env in normalize.normalize_whatsapp(json.loads(record["Sns"]["Message"])):
            pipeline.process(tbl, env)
            count += 1
    return {"processed": count}
