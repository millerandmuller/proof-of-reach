import os
import sys

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ["LEDGER_TABLE"] = "test-ledger"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def tbl():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="test-ledger",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield ddb.Table("test-ledger")


@pytest.fixture(autouse=True)
def single_thread_pools(monkeypatch):
    """moto's transact_write_items is not thread-safe (it copies the table while another thread writes).
    Real DynamoDB is. Production uses thread pools; tests run them with one worker."""
    from consumers import batch
    from engine import rule_engine
    monkeypatch.setattr(rule_engine, "MAX_WORKERS", 1)
    monkeypatch.setattr(batch, "THREADS", 1)
