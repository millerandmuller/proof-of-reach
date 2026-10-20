"""F2: RCS configuration-set event destination (SNS -> SQS) -> ledger. Delivered, read, failed, fallback."""
from common import normalize, pipeline
from consumers import batch


def handle(tbl, message, _sns_timestamp) -> int:
    env = normalize.normalize_rcs_status(message)
    if env is None:
        return 0
    pipeline.process(tbl, env)
    return 1


handler = batch.make_handler(handle)
