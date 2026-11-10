SECRET_KEY = "not-secret"
INSTALLED_APPS = ["django_s3_sqlite"]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
USE_TZ = True
