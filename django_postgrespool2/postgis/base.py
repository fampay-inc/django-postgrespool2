from ..base import DatabaseWrapperMixin

from django.contrib.gis.db.backends.postgis.base import DatabaseWrapper as Psycopg2DatabaseWrapper


class DatabaseWrapper(DatabaseWrapperMixin, Psycopg2DatabaseWrapper):
    pass
