import os
import sqlite3

import pytest
from botocore.exceptions import ClientError

from django_s3_sqlite import base

from .conftest import BUCKET, KEY


def execute(db, sql, *params):
    with db.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
    db.close()
    return rows


def count(db):
    return execute(db, "SELECT count(*) FROM t")[0][0]


def remote_etag(s3):
    return s3.head_object(Bucket=BUCKET, Key=KEY)["ETag"]


def put_remote(s3, tmp_path, *statements):
    """Simulate another container writing to the remote database."""
    path = tmp_path / "other.sqlite3"
    path.write_bytes(s3.get_object(Bucket=BUCKET, Key=KEY)["Body"].read())
    conn = sqlite3.connect(path)
    for sql in statements:
        conn.execute(sql)
    conn.commit()
    conn.close()
    s3.put_object(Bucket=BUCKET, Key=KEY, Body=path.read_bytes())


def test_missing_remote_starts_empty_and_read_does_not_create_it(make_db, s3):
    db = make_db()
    assert execute(db, "SELECT 1") == [(1,)]
    assert "Contents" not in s3.list_objects_v2(Bucket=BUCKET)


def test_write_is_pushed_and_visible_to_another_container(make_db):
    execute(make_db("a"), "CREATE TABLE t (x)")
    execute(make_db("a"), "INSERT INTO t VALUES (1)")
    assert count(make_db("b")) == 1


def test_read_only_request_does_not_push(make_db, s3):
    db = make_db()
    execute(db, "CREATE TABLE t (x)")
    etag = remote_etag(s3)
    for _ in range(3):
        assert count(db) == 0
    assert remote_etag(s3) == etag


def test_warm_connection_sees_remote_changes(make_db, s3, tmp_path):
    # Regression: the old backend only synced in __init__, so a warm
    # container served stale data forever and clobbered S3 on its next write.
    db = make_db()
    execute(db, "CREATE TABLE t (x)")
    put_remote(s3, tmp_path, "INSERT INTO t VALUES (1)")
    assert count(db) == 1


def test_unchanged_remote_is_not_redownloaded(make_db):
    db = make_db()
    execute(db, "CREATE TABLE t (x)")
    db2 = make_db("b")
    count(db2)
    inode = os.stat(db2.local_path).st_ino
    count(db2)
    count(db2)
    assert os.stat(db2.local_path).st_ino == inode


def test_cache_ttl_skips_revalidation(make_db, s3, tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(base.time, "monotonic", lambda: now[0])
    db = make_db(CACHE_TTL=30)
    execute(db, "CREATE TABLE t (x)")
    put_remote(s3, tmp_path, "INSERT INTO t VALUES (1)")

    now[0] += 10
    assert count(db) == 0  # within TTL, local copy trusted
    now[0] += 30
    assert count(db) == 1  # TTL expired, revalidated


def test_stale_write_is_rejected_not_clobbered(make_db, s3, caplog):
    execute(make_db("a"), "CREATE TABLE t (x)")
    a, b = make_db("a"), make_db("b")

    with b.cursor() as cursor:
        cursor.execute("SELECT 1")  # b's request starts and syncs
        execute(a, "INSERT INTO t VALUES ('a')")  # a writes meanwhile
        etag = remote_etag(s3)
        cursor.execute("INSERT INTO t VALUES ('b')")
    b.close()

    assert remote_etag(s3) == etag
    assert "changed since it was downloaded" in caplog.text
    # b throws away its conflicting copy and picks up a's write.
    assert execute(b, "SELECT x FROM t") == [("a",)]


def test_conditional_writes_can_be_disabled(make_db, s3):
    execute(make_db("a"), "CREATE TABLE t (x)")
    a, b = make_db("a"), make_db("b", CONDITIONAL_WRITES=False)

    with b.cursor() as cursor:
        cursor.execute("SELECT 1")
        execute(a, "INSERT INTO t VALUES ('a')")
        cursor.execute("INSERT INTO t VALUES ('b')")
    b.close()

    # Last writer wins, as it always used to.
    assert execute(make_db("c"), "SELECT x FROM t") == [("b",)]


def test_s3_failure_falls_back_to_synced_local_copy(make_db, monkeypatch):
    db = make_db()
    execute(db, "CREATE TABLE t (x)")

    def boom(**kwargs):
        raise ClientError({"Error": {"Code": "InternalError"}}, "GetObject")

    monkeypatch.setattr(db.s3, "get_object", boom)
    assert count(db) == 0


def test_s3_failure_without_local_copy_raises(make_db, monkeypatch):
    db = make_db()

    def boom(**kwargs):
        raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")

    monkeypatch.setattr(db.s3, "get_object", boom)
    with pytest.raises(ClientError):
        execute(db, "SELECT 1")


def test_close_inside_atomic_block_releases_without_pushing(make_db, s3):
    db = make_db()
    execute(db, "CREATE TABLE t (x)")
    etag = remote_etag(s3)

    db.ensure_connection()
    db.set_autocommit(False)
    with db.cursor() as cursor:
        cursor.execute("INSERT INTO t VALUES (1)")
    db.in_atomic_block = True
    db.close()  # rolls back; Django keeps the wrapper flagged closed_in_transaction

    assert remote_etag(s3) == etag
    assert db._local.open_connections == 0

    db.in_atomic_block = False
    db.connection = None
    assert count(db) == 0
    assert db._local.open_connections == 0


def test_settings_dict_is_not_mutated(make_db):
    settings = {"ENGINE": "django_s3_sqlite", "NAME": KEY, "BUCKET": BUCKET}
    base.DatabaseWrapper(settings)
    assert settings["NAME"] == KEY


def test_wal_mode_settles_after_a_write(make_db, s3):
    db = make_db(OPTIONS={"init_command": "PRAGMA journal_mode=WAL"})
    execute(db, "CREATE TABLE t (x)")
    execute(db, "INSERT INTO t VALUES (1)")
    count(db)  # may push once more after the checkpoint
    etag = remote_etag(s3)
    for _ in range(3):
        assert count(db) == 1
    assert remote_etag(s3) == etag
    assert count(make_db("b")) == 1
