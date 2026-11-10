from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS, connections


class Command(BaseCommand):
    """
    VACUUM the S3-stored SQLite database to keep it small and unfragmented.
    Worth running after deleting a lot of data.
    """

    help = "VACUUMs an S3-stored SQLite database to minimise size and fragmentation."

    def add_arguments(self, parser):
        parser.add_argument("--database", default=DEFAULT_DB_ALIAS)

    def handle(self, *args, **options):
        connection = connections[options["database"]]
        if connection.settings_dict["ENGINE"] != "django_s3_sqlite":
            raise CommandError(
                "This command is only for the 'django_s3_sqlite' database engine."
            )
        self.stdout.write("Starting database VACUUM...")
        with connection.cursor() as cursor:
            cursor.execute("VACUUM")
        # Closing pushes the result to S3, including under call_command().
        connection.close()
        self.stdout.write(self.style.SUCCESS("VACUUM complete."))
