"""F5: SES event publishing (SNS -> SQS) -> ledger. A click on the acknowledgment link is the acknowledgment."""
from common import normalize, pipeline
from consumers import batch


def handle(tbl, message, _sns_timestamp) -> int:
    env = normalize.normalize_ses(message)
    if env is None:
        return 0
    pipeline.process(tbl, env)
    return 1


handler = batch.make_handler(handle)
