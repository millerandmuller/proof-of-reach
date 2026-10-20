"""F2: RCS configuration-set event destination (SNS) -> ledger. Delivered, read, failed, fallback."""
import json

from common import ledger, normalize, pipeline


def handler(event, _context=None):
    tbl = ledger.table()
    results = []
    for record in event["Records"]:
        env = normalize.normalize_rcs_status(json.loads(record["Sns"]["Message"]))
        if env is not None:
            results.append(pipeline.process(tbl, env))
    return {"processed": len(results)}
