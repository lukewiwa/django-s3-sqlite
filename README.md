# django-s3-sqlite

[![PyPI](https://img.shields.io/pypi/v/django-s3-sqlite.svg)](https://pypi.python.org/pypi/django-s3-sqlite)

This project was inspired and started with [Zappa](https://github.com/Miserlou/Zappa). Thanks to [Rich Jones](https://github.com/Miserlou) for all of his amazing work.

## Installation

Install via `pip`:

    $ pip install django-s3-sqlite

Add to your installed apps:

    INSTALLED_APPS += ["django_s3_sqlite"]

## Using an S3-Backed Database Engine

`django-s3-sqlite` allows use of an [S3-synced SQLite database](https://blog.zappa.io/posts/s3sqlite-a-serverless-relational-database) as a Django database engine.

It scales very well for high-read applications that don't have concurrent writes (like a CMS for your blog), and it's orders of magnitude cheaper than AWS RDS or Aurora (pennies per month instead of many dollars per month). It is not a good fit for applications with concurrent writers; see [Writes](#writes).

#### Django Settings

```python
DATABASES = {
    "default": {
        "ENGINE": "django_s3_sqlite",
        "NAME": "sqlite.db",  # the object key in the bucket
        "BUCKET": "your-db-bucket",
        # Everything below is optional.
        "CACHE_TTL": 0,  # seconds to trust the local copy without asking S3
        "CONDITIONAL_WRITES": True,  # reject writes based on a stale copy
        "LOCAL_DIR": "/tmp",  # where the local copy lives
        "AWS_S3_ACCESS_KEY": "AKIA0000000000000000",  # to lock the bucket to an IAM user
        "AWS_S3_ACCESS_SECRET": "9tIZfakefakefakefakeT9Q6LD6jB5UyofakeISN",
        "REGION_NAME": "us-east-1",
        "ENDPOINT_URL": None,  # for S3-compatible stores, MinIO, LocalStack etc.
        "SIGNATURE_VERSION": "s3v4",
    }
}
```

Without explicit keys, boto3's normal credential chain is used (on Lambda, the function's role). The role needs `s3:GetObject` and `s3:PutObject` on the key.

#### How it works

Each process keeps a local copy of the database in `LOCAL_DIR`.

**Reads.** When Django opens a connection, the local copy is revalidated with a conditional `GET` using the ETag of the version we last saw. If nothing changed, S3 answers `304 Not Modified` and no data moves. If it did change, the new version is streamed to a temp file and swapped in atomically. Read-only requests never upload anything, and change detection is a `stat()` rather than hashing the file.

If even the revalidation round trip is too much, set `CACHE_TTL` to a number of seconds. Within that window the local copy is used without asking S3 at all, so a container may serve data up to that many seconds stale. With conditional writes on, that staleness can't turn into lost data: a write based on a stale copy is rejected (see below).

If S3 can't be reached but we already have a synced local copy, we log a warning and serve the local copy. If there's no local copy we raise, rather than starting from an empty database and later overwriting the real one.

#### Writes

When the last connection closes and the local file has changed, a consistent snapshot is taken with SQLite's backup API and uploaded with a conditional `PUT` (`If-Match` the ETag we downloaded, or `If-None-Match: *` for a brand new database).

If someone else wrote in the meantime, S3 rejects the upload: the write is discarded, an error is logged, and the next connection downloads the current version. This is still last-writer-loses, but it's loud and it never clobbers someone else's data. Set `CONDITIONAL_WRITES` to `False` for the old last-writer-wins behaviour, e.g. on an S3-compatible store that doesn't support conditional writes.

Don't bother with WAL mode; the whole file is shipped around anyway.

#### Runtime

Django 5.2+ needs SQLite 3.31+. The AWS Lambda Python 3.12+ runtimes (Amazon Linux 2023) ship a new enough SQLite, so no custom `_sqlite3.so` is needed any more. Use one of those.

#### Vacuum

Since SQLite keeps the database in a single file, you will want to keep it as small and defragmented as possible. It is good to occasionally perform a database vacuum, especially after deleting or updating data:

```bash
zappa manage [instance] s3_sqlite_vacuum
```

## Creating a Default Admin User 

You'll probably need a default user to manage your application with, so you can now:

    $ zappa manage <stage> create_admin_user

Or you can pass some arguments:
   
    $ zappa manage <stage> create_admin_user one two three

This will internally make this call:

```python
User.objects.create_superuser("one", "two", "three")
```

# Release Notes & Contributors

* Thank you to our [wonderful contributors](https://github.com/FlipperPA/django-s3-sqlite/graphs/contributors)!
* Release notes are [available on GitHub](https://github.com/FlipperPA/django-s3-sqlite/releases).

# Maintainers and Creator

* Maintainer: Tim Allen (https://github.com/FlipperPA/)
* Maintainer: Peter Baumgartner (https://github.com/ipmb/)
* Original Creator: Rich Jones (https://github.com/Miserlou/)

This package is largely maintained by the staff of [Wharton Research Data Services](https://wrds.wharton.upenn.edu/). We are thrilled that [The Wharton School](https://www.wharton.upenn.edu/) allows us a certain amount of time to contribute to open-source projects. We add features as they are necessary for our projects, and try to keep up with Issues and Pull Requests as best we can. Due to time constraints (our full time jobs!), Feature Requests without a Pull Request may not be implemented, but we are always open to new ideas and grateful for contributions and our package users.

# Development

```bash
pip install -e ".[test]"
pytest
```

The tests run against [moto](https://github.com/getmoto/moto), so no AWS account is needed.
