"""
A Django database backend that keeps a SQLite file in S3.

Each process keeps a local copy of the database. When a connection opens, the
local copy is revalidated against S3 with a conditional GET on the object's
ETag, so an unchanged database costs one 304 and no transfer. When the last
connection closes and the local file has changed, a consistent snapshot is
uploaded with a conditional PUT, so a stale copy can't silently overwrite
someone else's write.
"""

import logging
import os
import shutil
import sqlite3
import tempfile
import threading
import time
from dataclasses import dataclass, field
from functools import cache

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from django.core.exceptions import ImproperlyConfigured
from django.db.backends.sqlite3.base import DatabaseWrapper as SQLiteDatabaseWrapper

log = logging.getLogger(__name__)

_NOT_FOUND = {"NoSuchKey", "404", "NotFound"}
_CONFLICT = {"PreconditionFailed", "ConditionalRequestConflict"}


@dataclass
class _LocalCopy:
    """Per-process state for one local database file, shared across threads."""

    lock: threading.RLock = field(default_factory=threading.RLock)
    # True once the local file reflects a known remote state (or a known absence).
    synced: bool = False
    # ETag of the remote object the local file is based on.
    etag: str | None = None
    exists_remotely: bool = False
    # Stat fingerprint of the local file when it last matched the remote.
    fingerprint: tuple | None = None
    # time.monotonic() of the last successful revalidation.
    checked_at: float = 0.0
    open_connections: int = 0


_copies: dict[str, _LocalCopy] = {}
_copies_lock = threading.Lock()


def _local_copy(local_path: str) -> _LocalCopy:
    with _copies_lock:
        return _copies.setdefault(local_path, _LocalCopy())


def _stat(path: str):
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    return st.st_ino, st.st_size, st.st_mtime_ns


def _fingerprint(path: str) -> tuple:
    """Cheap change detection: stat the database and its WAL, no hashing."""
    return _stat(path), _stat(path + "-wal")


@cache
def _s3_client(access_key, secret_key, signature_version, endpoint_url, region_name):
    # Client creation is slow and clients are thread-safe, so share them.
    session = boto3.session.Session(
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
    )
    return session.client(
        "s3",
        endpoint_url=endpoint_url,
        region_name=region_name,
        config=Config(signature_version=signature_version),
    )


def _error_code(e: ClientError) -> tuple[str, int]:
    return (
        e.response.get("Error", {}).get("Code", ""),
        e.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0),
    )


class DatabaseWrapper(SQLiteDatabaseWrapper):
    """
    Wraps Django's SQLite backend in one that shuttles the database file to and
    from an S3 bucket.
    """

    def __init__(self, settings_dict, *args, **kwargs):
        # Django hands every thread's connection the same settings dict, so take
        # a copy before pointing NAME at the local file.
        settings_dict = dict(settings_dict)
        try:
            self.s3_bucket = settings_dict["BUCKET"]
            self.s3_key = settings_dict["NAME"]
        except KeyError as e:
            raise ImproperlyConfigured(f"django_s3_sqlite requires {e.args[0]}.") from e

        local_dir = settings_dict.get("LOCAL_DIR") or tempfile.gettempdir()
        self.local_path = os.path.join(local_dir, self.s3_key)
        settings_dict["NAME"] = self.local_path

        self.cache_ttl = float(settings_dict.get("CACHE_TTL", 0))
        self.conditional_writes = settings_dict.get("CONDITIONAL_WRITES", True)
        self.s3 = _s3_client(
            settings_dict.get("AWS_S3_ACCESS_KEY"),
            settings_dict.get("AWS_S3_ACCESS_SECRET"),
            settings_dict.get("SIGNATURE_VERSION", "s3v4"),
            settings_dict.get("ENDPOINT_URL"),
            settings_dict.get("REGION_NAME"),
        )
        self._local = _local_copy(self.local_path)
        self._holding_local = False
        super().__init__(settings_dict, *args, **kwargs)

    # Connection lifecycle

    def get_new_connection(self, conn_params):
        # The test runner swaps NAME for a throwaway database; leave that alone.
        if self._holding_local or self.settings_dict["NAME"] != self.local_path:
            return super().get_new_connection(conn_params)

        state = self._local
        with state.lock:
            # Never replace the file out from under another open connection.
            if state.open_connections == 0:
                self._refresh(state)
            state.open_connections += 1
        self._holding_local = True
        try:
            return super().get_new_connection(conn_params)
        except Exception:
            self._release_local(push=False)
            raise

    def close(self):
        super().close()
        # Inside an atomic block Django closes the underlying connection but
        # keeps self.connection around, flagged closed_in_transaction.
        closed = self.connection is None or self.closed_in_transaction
        if closed and self._holding_local:
            self._release_local(push=True)

    def _release_local(self, push):
        state = self._local
        with state.lock:
            self._holding_local = False
            state.open_connections -= 1
            if push and state.fingerprint != _fingerprint(self.local_path):
                self._push(state)
            else:
                log.debug("Database unchanged, not pushing to S3.")

    # Sync

    def _refresh(self, state: _LocalCopy):
        """Make sure the local file is the current remote version."""
        have_local = os.path.exists(self.local_path)
        if (
            state.synced
            and have_local
            and self.cache_ttl
            and time.monotonic() - state.checked_at < self.cache_ttl
        ):
            return

        kwargs = {"Bucket": self.s3_bucket, "Key": self.s3_key}
        if state.synced and state.etag and have_local:
            kwargs["IfNoneMatch"] = state.etag
        try:
            response = self.s3.get_object(**kwargs)
        except ClientError as e:
            code, status = _error_code(e)
            if status == 304:
                log.debug("Local database matches S3 (%s).", state.etag)
                state.checked_at = time.monotonic()
                return
            if code in _NOT_FOUND:
                log.info(
                    "s3://%s/%s doesn't exist, starting with an empty database.",
                    self.s3_bucket,
                    self.s3_key,
                )
                self._discard_local()
                # Create it now so merely opening a connection doesn't look
                # like a change worth pushing.
                os.makedirs(os.path.dirname(self.local_path), exist_ok=True)
                open(self.local_path, "wb").close()
                state.etag = None
                state.exists_remotely = False
                state.fingerprint = _fingerprint(self.local_path)
                state.synced = True
                state.checked_at = time.monotonic()
                return
            if state.synced and have_local:
                log.warning(
                    "Couldn't revalidate database against S3, using local copy.",
                    exc_info=True,
                )
                return
            raise

        self._write_local(response["Body"])
        state.etag = response["ETag"]
        state.exists_remotely = True
        state.fingerprint = _fingerprint(self.local_path)
        state.synced = True
        state.checked_at = time.monotonic()
        log.debug("Downloaded database from S3 (%s).", state.etag)

    def _push(self, state: _LocalCopy):
        """Upload a consistent snapshot of the local database to S3."""
        if not state.synced:
            log.error("Local database is out of sync with S3, refusing to push it.")
            return

        before = _fingerprint(self.local_path)
        kwargs = {"Bucket": self.s3_bucket, "Key": self.s3_key}
        if self.conditional_writes:
            if state.exists_remotely:
                kwargs["IfMatch"] = state.etag
            else:
                kwargs["IfNoneMatch"] = "*"

        snapshot = None
        try:
            snapshot = self._snapshot()
            with open(snapshot, "rb") as f:
                response = self.s3.put_object(Body=f, **kwargs)
        except ClientError as e:
            code, status = _error_code(e)
            if code in _CONFLICT or status in (409, 412):
                log.error(
                    "s3://%s/%s changed since it was downloaded; this write has "
                    "been discarded. Concurrent writers aren't supported.",
                    self.s3_bucket,
                    self.s3_key,
                )
                # Force a full download next time a connection opens.
                state.synced = False
                state.etag = None
            else:
                log.exception("Couldn't push database to S3, will retry on next close.")
            return
        except Exception:
            log.exception("Couldn't push database to S3, will retry on next close.")
            return
        finally:
            if snapshot:
                os.remove(snapshot)

        state.etag = response["ETag"]
        state.exists_remotely = True
        state.fingerprint = before
        state.checked_at = time.monotonic()
        log.debug("Pushed database to S3 (%s).", state.etag)

    # Local file handling

    def _temp_path(self) -> str:
        directory = os.path.dirname(self.local_path)
        os.makedirs(directory, exist_ok=True)
        fd, path = tempfile.mkstemp(dir=directory, suffix=".tmp")
        os.close(fd)
        return path

    def _write_local(self, body):
        """Stream the download to a temp file and swap it in atomically."""
        tmp = self._temp_path()
        try:
            with open(tmp, "wb") as f:
                shutil.copyfileobj(body, f, length=1024 * 1024)
            self._discard_local(keep_main=True)
            os.replace(tmp, self.local_path)
        except BaseException:
            os.remove(tmp)
            raise

    def _discard_local(self, keep_main=False):
        suffixes = ("-journal", "-wal", "-shm") + (() if keep_main else ("",))
        for suffix in suffixes:
            try:
                os.remove(self.local_path + suffix)
            except FileNotFoundError:
                pass

    def _snapshot(self) -> str:
        """Copy the database with SQLite's backup API so we never upload a
        half-written file, whatever the journal mode."""
        tmp = self._temp_path()
        try:
            src = sqlite3.connect(self.local_path)
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
        except BaseException:
            os.remove(tmp)
            raise
        return tmp
