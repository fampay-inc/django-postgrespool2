import datetime
import logging
from functools import partial
from importlib import import_module

from django.conf import settings
from django.db.backends.postgresql.base import (
    psycopg2,
    Database,
)
from django.db.backends.postgresql.creation import DatabaseCreation as Psycopg2DatabaseCreation
from django.dispatch import Signal
from django.utils import version
try:
    from django.utils.asyncio import async_unsafe
except ImportError:
    # dummy decorator
    def async_unsafe(func):
        return func
try:
    # django 2.2
    from django.db.backends.postgresql.utils import utc_tzinfo_factory
    from django.utils.timezone import utc

    def utc_tzinfo_factory(offset):
        zero = 0
        # psycopg>=2.9 sends offset as timedelta
        if isinstance(offset, datetime.timedelta):
            zero = datetime.timedelta()
        if offset != zero:
            raise AssertionError("database connection isn't set to UTC")
        return utc

except ImportError:
    utc_tzinfo_factory = None
from sqlalchemy import event
from sqlalchemy.dialects import postgresql
from sqlalchemy.pool import manage

# DATABASE_POOL_ARGS should be something like:
# {'max_overflow':10, 'pool_size':5, 'recycle':300}
pool_args = {'max_overflow': 10, 'pool_size': 5, 'recycle': 300}
pool_args.update(getattr(settings, 'DATABASE_POOL_ARGS', {}))
dialect = postgresql.dialect(dbapi=psycopg2)
pool_args['dialect'] = dialect

POOL_CLS = getattr(settings, 'DATABASE_POOL_CLASS', 'sqlalchemy.pool.QueuePool')
pool_module_name, pool_cls_name = POOL_CLS.rsplit('.', 1)
pool_cls = getattr(import_module(pool_module_name), pool_cls_name)
pool_args['poolclass'] = pool_cls

db_pool = manage(Database, **pool_args)
pool_disposed = Signal()

log = logging.getLogger('z.pool')

django_version = version.get_version_tuple(version.get_version())

def _log(message, *args):
    log.debug(message)


# Only hook up the listeners if we are in debug mode.
if settings.DEBUG:
    event.listen(pool_cls, 'checkout', partial(_log, 'retrieved from pool'))
    event.listen(pool_cls, 'checkin', partial(_log, 'returned to pool'))
    event.listen(pool_cls, 'connect', partial(_log, 'new connection'))


class DatabaseCreation(Psycopg2DatabaseCreation):
    def _clone_test_db(self, *args, **kw):
        self.connection.dispose()
        super(DatabaseCreation, self)._clone_test_db(*args, **kw)

    def create_test_db(self, *args, **kw):
        self.connection.dispose()
        super(DatabaseCreation, self).create_test_db(*args, **kw)

    def destroy_test_db(self, *args, **kw):
        """Ensure connection pool is disposed before trying to drop database.
        """
        self.connection.dispose()
        super(DatabaseCreation, self).destroy_test_db(*args, **kw)


class DatabaseWrapperMixin:
    """SQLAlchemy FTW."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pool = None
        self._pool_connection = None
        self.creation = DatabaseCreation(self)

    @property
    def pool(self):
        return self._pool

    def _close(self):
        if self._pool_connection is not None:
            if not self.is_usable():
                self._pool_connection.invalidate()
            with self.wrap_database_errors:
                return self._pool_connection.close()

    @async_unsafe
    def create_cursor(self, name=None):
        if name:
            # In autocommit mode, the cursor will be used outside of a
            # transaction, hence use a holdable cursor.
            cursor = self._pool_connection.cursor(
                name, scrollable=False, withhold=self.connection.autocommit)
        else:
            cursor = self._pool_connection.cursor()
        cursor.tzinfo_factory = self.tzinfo_factory if settings.USE_TZ else None
        return cursor

    def tzinfo_factory(self, offset):
        if utc_tzinfo_factory:
            # for Django 2.2
            return utc_tzinfo_factory(offset)
        return self.timezone

    def dispose(self):
        """
        Dispose of the pool for this instance, closing all connections.
        """
        self.close()
        self._pool_connection = None
        # _DBProxy.dispose doesn't actually call dispose on the pool
        if self.pool:
            self.pool.dispose()
            self._pool = None
        conn_params = self.get_connection_params()
        db_pool.dispose(**conn_params)
        pool_disposed.send(sender=self.__class__, connection=self)

    @async_unsafe
    def get_new_connection(self, conn_params):
        if not self._pool:
            self._pool = db_pool.get_pool(**conn_params)
        # get new connection through pool, not creating a new one outside.
        self._pool_connection = self.pool.connect()
        c = self._pool_connection.connection  # dbapi connection

        options = self.settings_dict['OPTIONS']
        try:
            self.isolation_level = options['isolation_level']
        except KeyError:
            self.isolation_level = c.isolation_level
        else:
            # Set the isolation level to the value from OPTIONS.
            if self.isolation_level != c.isolation_level:
                c.set_session(isolation_level=self.isolation_level)

        if django_version >= (3, 1, 1):
            psycopg2.extras.register_default_jsonb(conn_or_curs=c, loads=lambda x: x)
        return c

    def is_usable(self):
        if not self.connection:
            return False
        return self.connection.closed == 0
