"""F3: RCS two-way SNS topic -> ledger. A suggestion tap is the acknowledgment; free text is a reply."""
import json

from common import ledger, normalize, pipeline


def handler(event, _context=None):
    tbl = ledger.table()
    results = []
    for record in event["Records"]:
        env = normalize.normalize_rcs_inbound(json.loads(record["Sns"]["Message"]), record["Sns"]["Timestamp"])
        if env is not None:
            results.append(pipeline.process(tbl, env))
    return {"processed": len(results)}
