"""F3: RCS two-way topic (SNS -> SQS) -> ledger. A suggestion tap is the acknowledgment; free text is a reply."""
from common import normalize, pipeline
from consumers import batch


def handle(tbl, message, sns_timestamp) -> int:
    env = normalize.normalize_rcs_inbound(message, sns_timestamp)
    if env is None:
        return 0
    pipeline.process(tbl, env)
    return 1


handler = batch.make_handler(handle)
