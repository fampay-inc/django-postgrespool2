from ..base import DatabaseWrapperMixin

from django.db.backends.postgresql.base import DatabaseWrapper as Psycopg2DatabaseWrapper


class DatabaseWrapper(DatabaseWrapperMixin, Psycopg2DatabaseWrapper):
    pass
