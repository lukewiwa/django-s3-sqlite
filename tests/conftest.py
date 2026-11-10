import boto3
import pytest
from moto import mock_aws

from django_s3_sqlite import base

BUCKET = "test-bucket"
KEY = "db.sqlite3"


@pytest.fixture(autouse=True)
def _unblock_db(django_db_blocker):
    # These tests build their own backend instances against moto, not the
    # test database pytest-django manages.
    with django_db_blocker.unblock():
        yield


@pytest.fixture(autouse=True)
def s3(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    base._copies.clear()
    base._s3_client.cache_clear()
    with mock_aws():
        client = boto3.client("s3")
        client.create_bucket(Bucket=BUCKET)
        yield client


@pytest.fixture
def make_db(tmp_path):
    """Build a backend instance. Each `container` gets its own local directory,
    standing in for a separate Lambda instance."""

    def make(container="a", **extra):
        settings = {
            "ENGINE": "django_s3_sqlite",
            "NAME": KEY,
            "BUCKET": BUCKET,
            "LOCAL_DIR": str(tmp_path / container),
            "OPTIONS": {},
            "TIME_ZONE": None,
            "AUTOCOMMIT": True,
            "ATOMIC_REQUESTS": False,
            "CONN_MAX_AGE": 0,
            "CONN_HEALTH_CHECKS": False,
            "TEST": {},
            **extra,
        }
        return base.DatabaseWrapper(settings, alias=f"s3-{container}")

    return make
