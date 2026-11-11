# django-s3-sqlite

[![PyPI](https://img.shields.io/pypi/v/django-s3-sqlite.svg)](https://pypi.org/project/django-s3-sqlite/)
[![CI](https://github.com/lukewiwa/django-s3-sqlite/actions/workflows/ci.yml/badge.svg)](https://github.com/lukewiwa/django-s3-sqlite/actions/workflows/ci.yml)

A Django database backend that keeps its SQLite file in S3.

It's a good fit for read-heavy sites with the occasional writer (a blog, a CMS, an internal tool) running on AWS Lambda or similar, where it costs pennies a month instead of an RDS instance. It's a bad fit for anything with concurrent writers. See [Writes](#writes) for exactly what happens there.

Requires Python 3.10+, Django 5.2+ and boto3 1.36+.

## Installation

```bash
uv add django-s3-sqlite
```

or `pip install django-s3-sqlite`. Then:

```python
INSTALLED_APPS += ["django_s3_sqlite"]

DATABASES = {
    "default": {
        "ENGINE": "django_s3_sqlite",
        "NAME": "sqlite.db",  # the object key in the bucket
        "BUCKET": "your-db-bucket",
    }
}
```

The app only needs to be in `INSTALLED_APPS` for its management commands; the backend works without it.

### Options

All optional, defaults shown.

```python
DATABASES = {
    "default": {
        "ENGINE": "django_s3_sqlite",
        "NAME": "sqlite.db",
        "BUCKET": "your-db-bucket",
        # Seconds to trust the local copy without asking S3. See Reads.
        "CACHE_TTL": 0,
        # Reject writes based on a stale copy instead of clobbering. See Writes.
        "CONDITIONAL_WRITES": True,
        # Where the local copy lives. NAME is resolved inside it.
        "LOCAL_DIR": tempfile.gettempdir(),
        # S3 client.
        "REGION_NAME": None,
        "ENDPOINT_URL": None,  # MinIO, LocalStack, R2 etc.
        "SIGNATURE_VERSION": "s3v4",
        "CONNECT_TIMEOUT": 5,
        "READ_TIMEOUT": 60,
        "MAX_ATTEMPTS": 5,  # botocore "standard" retry mode
        # Static credentials, if you want the bucket locked to one IAM user.
        "AWS_S3_ACCESS_KEY": None,
        "AWS_S3_ACCESS_SECRET": None,
    }
}
```

Without static credentials boto3's normal chain applies, which on Lambda means the function's role. It needs `s3:GetObject` and `s3:PutObject` on the key.

### Runtime

Django 5.2 needs SQLite 3.31+. On Lambda, use a Python 3.12+ runtime: they're Amazon Linux 2023 and ship a new enough SQLite. The old advice about dropping a custom `_sqlite3.so` into your project no longer applies.

## How it works

Each process keeps a local copy of the database in `LOCAL_DIR`, plus the ETag of the S3 object it came from.

### Reads

Every time Django opens a connection (by default, once per request) the local copy is revalidated with a conditional `GET` on that ETag:

- Unchanged: S3 answers `304 Not Modified`. No data moves.
- Changed: the new version streams to a temp file, is verified against S3's stored checksum (where it has one) and checked for a SQLite header, then swapped in atomically.
- Missing: you start with an empty database, and nothing is created in S3 until you actually write something.

A read-only request costs one small S3 round trip and a `stat()`. Nothing is hashed and nothing is uploaded.

If that round trip still matters, set `CACHE_TTL`. Within that many seconds of the last check, the local copy is used without asking S3 at all, so a container can serve data up to `CACHE_TTL` seconds stale. Because writes are conditional, that staleness can't turn into lost data.

If S3 is unreachable and we have a synced local copy, we log a warning and serve it. If there's no local copy we raise, rather than quietly starting from an empty database and later overwriting the real one with it.

The local copy is only ever replaced when no other connection in the process has it open.

### Writes

When a connection closes and the local file has changed, we:

1. take a consistent snapshot with SQLite's backup API, so a half-written file is never uploaded;
2. run `PRAGMA quick_check` on the snapshot, so a corrupt file is never uploaded over a good one;
3. upload it with a conditional `PUT`: `If-Match` the ETag we downloaded, or `If-None-Match: *` for a brand-new database.

If someone else wrote in the meantime, S3 rejects the upload with `412`. The write is discarded, an error is logged, and the next connection downloads the current version. So concurrent writes still lose, but they lose loudly, and they never clobber someone else's data.

If the upload fails for any other reason (network, throttling), the local changes are kept and retried when the next connection closes. If S3 moves on before that retry succeeds, the local changes are discarded and an error is logged.

`CONDITIONAL_WRITES = False` brings back the old last-writer-wins behaviour, for S3-compatible stores that don't support conditional writes.

Don't bother with WAL mode; the whole file gets shipped around regardless.

### Caveats

- Changes reach S3 when a connection *closes*. Django closes connections at the end of each request, and `manage.py` closes them on exit. `call_command()` doesn't, and Zappa's `manage` uses it, so a `zappa manage <stage> migrate` reaches S3 when the next request finishes. `s3_sqlite_vacuum` closes its connection itself.
- The database is uploaded whole on every write. Keep it small.
- The test runner's throwaway database never touches S3.

## Management commands

Since SQLite keeps everything in one file, vacuum it now and then, particularly after deleting a lot of data:

```bash
zappa manage <stage> s3_sqlite_vacuum
```

To create a superuser with a random password:

```bash
zappa manage <stage> create_admin_user
```

Or with explicit arguments, passed straight to `User.objects.create_superuser()`:

```bash
zappa manage <stage> create_admin_user username email password
```

## Development

```bash
uv sync
```

```bash
uv run pytest
```

```bash
uv run ruff check && uv run ruff format --check
```

The tests run against [moto](https://github.com/getmoto/moto), so you don't need an AWS account. They cover the conflict, retry and corruption paths, and include a race between several simulated containers to check that every lost write is reported.

CI tests every supported Python and Django combination, plus the lowest versions `pyproject.toml` allows.

## Releasing

Versions come from git tags via `hatch-vcs`. To release:

1. Create a GitHub release with a tag like `v0.1.0`.
2. The `Release` workflow runs the full CI, checks the built version matches the tag, and publishes to PyPI with [trusted publishing](https://docs.pypi.org/trusted-publishers/). No API tokens are involved.

One-off setup:

- On PyPI, under the project's *Publishing* settings, add a GitHub trusted publisher: this repository, workflow `release.yml`, environment `pypi`.
- In GitHub, create an environment called `pypi`. Adding yourself as a required reviewer makes every release wait for a click before it goes out.

## Credits

Originally created by [Rich Jones](https://github.com/Miserlou) as part of [Zappa](https://github.com/zappa/Zappa), then maintained by [Tim Allen](https://github.com/FlipperPA) and [Peter Baumgartner](https://github.com/ipmb) with support from [Wharton Research Data Services](https://wrds.wharton.upenn.edu/). Now maintained by [Luke Wiwatowski](https://github.com/lukewiwa).

MIT licensed. See [LICENSE](LICENSE).
