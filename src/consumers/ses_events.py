"""F5: SES event publishing (SNS) -> ledger. A click on the acknowledgment link is the acknowledgment."""
import json

from common import ledger, normalize, pipeline


def handler(event, _context=None):
    tbl = ledger.table()
    results = []
    for record in event["Records"]:
        env = normalize.normalize_ses(json.loads(record["Sns"]["Message"]))
        if env is not None:
            results.append(pipeline.process(tbl, env))
    return {"processed": len(results)}
