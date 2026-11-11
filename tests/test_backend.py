import os
import sqlite3
import threading

import pytest
from botocore.exceptions import ClientError
from django.core.exceptions import ImproperlyConfigured

from django_s3_sqlite import base

from .conftest import BUCKET, KEY

FILL = (
    "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < {n}) "
    "INSERT INTO t SELECT randomblob(1000) FROM n"
)


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


def test_non_sqlite_object_is_refused(make_db, s3):
    s3.put_object(Bucket=BUCKET, Key=KEY, Body=b"<html>definitely not a database")
    with pytest.raises(base.InvalidDatabaseError):
        execute(make_db(), "SELECT 1")


@pytest.mark.parametrize("name", ["../escape.db", "a/../../escape.db"])
def test_name_cannot_escape_local_dir(make_db, name):
    with pytest.raises(ImproperlyConfigured):
        make_db(NAME=name)


def test_absolute_name_stays_inside_local_dir(make_db, tmp_path):
    db = make_db(NAME="/nested/db.sqlite3")
    assert db.local_path == str(tmp_path / "a" / "nested" / "db.sqlite3")
    execute(db, "CREATE TABLE t (x)")
    assert count(make_db("b", NAME="/nested/db.sqlite3")) == 0


def test_corrupt_local_database_is_not_pushed(make_db, s3, caplog):
    db = make_db()
    execute(db, "CREATE TABLE t (x)")
    execute(db, FILL.format(n=50))
    etag = remote_etag(s3)

    # Scribble over the table's pages behind SQLite's back.
    with open(db.local_path, "r+b") as f:
        f.seek(4096)
        f.write(os.urandom(4096 * 4))
    with db._local.lock:
        db._push(db._local)

    assert remote_etag(s3) == etag
    assert "not pushing" in caplog.text


def test_threads_in_one_process_never_lose_writes(make_db, s3):
    execute(make_db(), "CREATE TABLE t (x)")
    execute(make_db(), "INSERT INTO t VALUES (0)")

    def work():
        db = make_db()
        for _ in range(10):
            execute(db, "UPDATE t SET x = x + 1")

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert execute(make_db("fresh"), "SELECT x FROM t") == [(80,)]


def test_racing_containers_lose_writes_loudly_not_silently(make_db, s3, caplog):
    execute(make_db("seed"), "CREATE TABLE t (x)")
    execute(make_db("seed"), "INSERT INTO t VALUES (0)")
    attempts = 0
    lock = threading.Lock()
    barrier = threading.Barrier(6)

    def work(name):
        nonlocal attempts
        db = make_db(name)
        barrier.wait()
        for _ in range(5):
            execute(db, "UPDATE t SET x = x + 1")
            with lock:
                attempts += 1

    threads = [threading.Thread(target=work, args=(f"c{i}",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    conflicts = caplog.text.count("changed since it was downloaded")
    final = execute(make_db("fresh"), "SELECT x FROM t")[0][0]
    # Every increment either landed or was reported. None vanished quietly.
    assert conflicts > 0, "no contention, test proves nothing"
    assert final == attempts - conflicts


def test_vacuum_command_pushes(make_db, s3, monkeypatch):
    from django.core.management import call_command

    from django_s3_sqlite.management.commands import s3_sqlite_vacuum

    db = make_db()
    execute(db, "CREATE TABLE t (x)")
    execute(db, FILL.format(n=200))
    execute(db, "DELETE FROM t")
    before = s3.head_object(Bucket=BUCKET, Key=KEY)["ContentLength"]

    monkeypatch.setattr(s3_sqlite_vacuum, "connections", {"default": db})
    call_command("s3_sqlite_vacuum")

    assert s3.head_object(Bucket=BUCKET, Key=KEY)["ContentLength"] < before


def fail_once(monkeypatch, client, method, exc):
    real = getattr(client, method)
    calls = []

    def flaky(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise exc
        return real(**kwargs)

    monkeypatch.setattr(client, method, flaky)
    return calls


S3_DOWN = ClientError({"Error": {"Code": "InternalError"}}, "PutObject")


@pytest.mark.parametrize("missing", ["BUCKET", "NAME"])
def test_missing_settings_are_reported(make_db, missing):
    with pytest.raises(ImproperlyConfigured, match=missing):
        make_db(**{missing: ""})


def test_failed_first_push_of_new_database_is_retried(make_db, monkeypatch, caplog):
    # Regression: the retry used to hit a 404 and wipe the unpushed file.
    db = make_db()
    fail_once(monkeypatch, db.s3, "put_object", S3_DOWN)
    execute(db, "CREATE TABLE t (x)")
    assert "will retry" in caplog.text

    assert execute(db, "SELECT 1") == [(1,)]  # read-only request retries the push
    assert count(make_db("b")) == 0


@pytest.mark.parametrize("exc", [S3_DOWN, OSError("disk on fire")])
def test_failed_push_is_retried_on_next_close(make_db, s3, monkeypatch, caplog, exc):
    db = make_db()
    execute(db, "CREATE TABLE t (x)")
    fail_once(monkeypatch, db.s3, "put_object", exc)
    execute(db, "INSERT INTO t VALUES (1)")
    assert count(make_db("b")) == 0

    count(db)
    assert count(make_db("c")) == 1


def test_unpushed_changes_overtaken_by_remote_are_reported(
    make_db, monkeypatch, caplog
):
    execute(make_db("b"), "CREATE TABLE t (x)")
    a = make_db("a")
    count(a)
    fail_once(monkeypatch, a.s3, "put_object", S3_DOWN)
    execute(a, "INSERT INTO t VALUES ('a')")
    execute(make_db("b"), "INSERT INTO t VALUES ('b')")

    assert execute(a, "SELECT x FROM t") == [("b",)]
    assert "changed before local changes could be pushed" in caplog.text


def test_out_of_sync_copy_is_never_pushed(make_db, s3, caplog):
    execute(make_db("b"), "CREATE TABLE t (x)")
    a1, a2 = make_db("a"), make_db("a")  # two threads in one container

    with a1.cursor() as c1:
        c1.execute("SELECT 1")
        execute(make_db("b"), "INSERT INTO t VALUES ('b')")
        execute(a2, "INSERT INTO t VALUES ('a2')")  # conflicts
        c1.execute("INSERT INTO t VALUES ('a1')")
    a1.close()

    assert "refusing to push" in caplog.text
    assert execute(make_db("c"), "SELECT x FROM t") == [("b",)]
    assert execute(a1, "SELECT x FROM t") == [("b",)]


def test_vacuum_refuses_other_engines(monkeypatch):
    from django.core.management import CommandError, call_command
    from django.db import connections

    from django_s3_sqlite.management.commands import s3_sqlite_vacuum

    monkeypatch.setattr(s3_sqlite_vacuum, "connections", connections)
    with pytest.raises(CommandError):
        call_command("s3_sqlite_vacuum")
