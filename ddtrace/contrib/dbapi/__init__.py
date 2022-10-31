"""
Generic dbapi tracing code.
"""
from typing import TYPE_CHECKING

import six

from ddtrace import config

from ...constants import ANALYTICS_SAMPLE_RATE_KEY
from ...constants import COMPONENT
from ...constants import SPAN_CLIENT
from ...constants import SPAN_KIND
from ...constants import SPAN_MEASURED_KEY
from ...ext import SpanTypes
from ...ext import sql
from ...internal.compat import PY2
from ...internal.logger import get_logger
from ...internal.utils import ArgumentError
from ...internal.utils import get_argument_value
from ...internal.utils import set_argument_value
from ...pin import Pin
from ...settings import _database_monitoring
from ...vendor import wrapt
from ..trace_utils import ext_service
from ..trace_utils import iswrapped


if TYPE_CHECKING:
    from typing import Any
    from typing import Dict
    from typing import Tuple


log = get_logger(__name__)


config._add(
    "dbapi2",
    dict(
        _default_service="db",
        _dbapi_span_name_prefix="sql",
        trace_fetch_methods=None,  # Part of the API. Should be implemented at the integration level.
    ),
)


class TracedCursor(wrapt.ObjectProxy):
    """TracedCursor wraps a psql cursor and traces its queries."""

    def __init__(self, cursor, pin, cfg):
        super(TracedCursor, self).__init__(cursor)
        pin.onto(self)
        # Allow dbapi-based integrations to override default span name prefix
        span_name_prefix = (
            cfg._dbapi_span_name_prefix
            if cfg and "_dbapi_span_name_prefix" in cfg
            else config.dbapi2._dbapi_span_name_prefix
        )
        self._self_datadog_name = "{}.query".format(span_name_prefix)
        self._self_last_execute_operation = None
        self._self_config = cfg or config.dbapi2

    def __iter__(self):
        return self.__wrapped__.__iter__()

    def __next__(self):
        return self.__wrapped__.__next__()

    if PY2:

        # Python 2 iterators use `next`
        def next(self):  # noqa: A001
            return self.__wrapped__.next()

    def _trace_method(self, method, name, resource, extra_tags, *args, **kwargs):
        """
        Internal function to trace the call to the underlying cursor method
        :param method: The callable to be wrapped
        :param name: The name of the resulting span.
        :param resource: The sql query. Sql queries are obfuscated on the agent side.
        :param extra_tags: A dict of tags to store into the span's meta
        :param args: The args that will be passed as positional args to the wrapped method
        :param kwargs: The args that will be passed as kwargs to the wrapped method
        :return: The result of the wrapped method invocation
        """
        pin = Pin.get_from(self)
        if not pin or not pin.enabled():
            return method(*args, **kwargs)
        measured = name == self._self_datadog_name

        with pin.tracer.trace(
            name, service=ext_service(pin, self._self_config), resource=resource, span_type=SpanTypes.SQL
        ) as s:
            if measured:
                s.set_tag(SPAN_MEASURED_KEY)
            # No reason to tag the query since it is set as the resource by the agent. See:
            # https://github.com/DataDog/datadog-trace-agent/blob/bda1ebbf170dd8c5879be993bdd4dbae70d10fda/obfuscate/sql.go#L232
            s.set_tags(pin.tags)
            s.set_tags(extra_tags)

            # set component tag equal to name of integration
            s.set_tag_str(COMPONENT, self._self_config.integration_name)

            # set span.kind to the type of request being performed
            s.set_tag_str(SPAN_KIND, SPAN_CLIENT)

            # set analytics sample rate if enabled but only for non-FetchTracedCursor
            if not isinstance(self, FetchTracedCursor):
                s.set_tag(ANALYTICS_SAMPLE_RATE_KEY, self._self_config.get_analytics_sample_rate())

            args, kwargs = _inject_dbm_comment_in_query_arg(s, args, kwargs)
            try:
                return method(*args, **kwargs)
            finally:
                # Try to fetch custom properties that were passed by the specific Database implementation
                self._set_post_execute_tags(s)

    def executemany(self, query, *args, **kwargs):
        """Wraps the cursor.executemany method"""
        self._self_last_execute_operation = query
        # Always return the result as-is
        # DEV: Some libraries return `None`, others `int`, and others the cursor objects
        #      These differences should be overridden at the integration specific layer (e.g. in `sqlite3/patch.py`)
        # FIXME[matt] properly handle kwargs here. arg names can be different
        # with different libs.
        return self._trace_method(
            self.__wrapped__.executemany,
            self._self_datadog_name,
            query,
            {"sql.executemany": "true"},
            query,
            *args,
            **kwargs
        )

    def execute(self, query, *args, **kwargs):
        """Wraps the cursor.execute method"""
        self._self_last_execute_operation = query

        # Always return the result as-is
        # DEV: Some libraries return `None`, others `int`, and others the cursor objects
        #      These differences should be overridden at the integration specific layer (e.g. in `sqlite3/patch.py`)
        return self._trace_method(self.__wrapped__.execute, self._self_datadog_name, query, {}, query, *args, **kwargs)

    def callproc(self, proc, *args):
        """Wraps the cursor.callproc method"""
        self._self_last_execute_operation = proc
        return self._trace_method(self.__wrapped__.callproc, self._self_datadog_name, proc, {}, proc, *args)

    def _set_post_execute_tags(self, span):
        row_count = self.__wrapped__.rowcount
        span.set_metric("db.rowcount", row_count)
        # Necessary for django integration backward compatibility. Django integration used to provide its own
        # implementation of the TracedCursor, which used to store the row count into a tag instead of
        # as a metric. Such custom implementation has been replaced by this generic dbapi implementation and
        # this tag has been added since.
        # Check row count is an integer type to avoid comparison type error
        if isinstance(row_count, six.integer_types) and row_count >= 0:
            span.set_tag(sql.ROWS, row_count)

    def __enter__(self):
        # previous versions of the dbapi didn't support context managers. let's
        # reference the func that would be called to ensure that errors
        # messages will be the same.
        self.__wrapped__.__enter__

        # and finally, yield the traced cursor.
        return self


class FetchTracedCursor(TracedCursor):
    """
    Sub-class of :class:`TracedCursor` that also instruments `fetchone`, `fetchall`, and `fetchmany` methods.

    We do not trace these functions by default since they can get very noisy (e.g. `fetchone` with 100k rows).
    """

    def fetchone(self, *args, **kwargs):
        """Wraps the cursor.fetchone method"""
        span_name = "{}.{}".format(self._self_datadog_name, "fetchone")
        return self._trace_method(
            self.__wrapped__.fetchone, span_name, self._self_last_execute_operation, {}, *args, **kwargs
        )

    def fetchall(self, *args, **kwargs):
        """Wraps the cursor.fetchall method"""
        span_name = "{}.{}".format(self._self_datadog_name, "fetchall")
        return self._trace_method(
            self.__wrapped__.fetchall, span_name, self._self_last_execute_operation, {}, *args, **kwargs
        )

    def fetchmany(self, *args, **kwargs):
        """Wraps the cursor.fetchmany method"""
        span_name = "{}.{}".format(self._self_datadog_name, "fetchmany")
        # We want to trace the information about how many rows were requested. Note that this number may be larger
        # the number of rows actually returned if less then requested are available from the query.
        size_tag_key = "db.fetch.size"

        try:
            extra_tags = {size_tag_key: get_argument_value(args, kwargs, 0, "size")}
        except ArgumentError:
            default_array_size = getattr(self.__wrapped__, "arraysize", None)
            extra_tags = {size_tag_key: default_array_size} if default_array_size else {}

        return self._trace_method(
            self.__wrapped__.fetchmany, span_name, self._self_last_execute_operation, extra_tags, *args, **kwargs
        )


class TracedConnection(wrapt.ObjectProxy):
    """TracedConnection wraps a Connection with tracing code."""

    def __init__(self, conn, pin=None, cfg=None, cursor_cls=None):
        if not cfg:
            cfg = config.dbapi2
        # Set default cursor class if one was not provided
        if not cursor_cls:
            # Do not trace `fetch*` methods by default
            cursor_cls = FetchTracedCursor if cfg.trace_fetch_methods else TracedCursor

        super(TracedConnection, self).__init__(conn)
        name = _get_vendor(conn)
        self._self_datadog_name = "{}.connection".format(name)
        db_pin = pin or Pin(service=name)
        db_pin.onto(self)
        # wrapt requires prefix of `_self` for attributes that are only in the
        # proxy (since some of our source objects will use `__slots__`)
        self._self_cursor_cls = cursor_cls
        self._self_config = cfg

    def __enter__(self):
        """Context management is not defined by the dbapi spec.

        This means unfortunately that the database clients each define their own
        implementations.

        The ones we know about are:

        - mysqlclient<2.0 which returns a cursor instance. >=2.0 returns a
          connection instance.
        - psycopg returns a connection.
        - pyodbc returns a connection.
        - pymysql doesn't implement it.
        - sqlite3 returns the connection.
        """
        r = self.__wrapped__.__enter__()

        if hasattr(r, "cursor"):
            # r is Connection-like.
            if r is self.__wrapped__:
                # Return the reference to this proxy object. Returning r would
                # return the untraced reference.
                return self
            else:
                # r is a different connection object.
                # This should not happen in practice but play it safe so that
                # the original functionality is maintained.
                return r
        elif hasattr(r, "execute"):
            # r is Cursor-like.
            if iswrapped(r):
                return r
            else:
                pin = Pin.get_from(self)
                if not pin:
                    return r
                return self._self_cursor_cls(r, pin, self._self_config)
        else:
            # Otherwise r is some other object, so maintain the functionality
            # of the original.
            return r

    def _trace_method(self, method, name, extra_tags, *args, **kwargs):
        pin = Pin.get_from(self)
        if not pin or not pin.enabled():
            return method(*args, **kwargs)

        with pin.tracer.trace(name, service=ext_service(pin, self._self_config)) as s:
            # set component tag equal to name of integration
            s.set_tag_str(COMPONENT, self._self_config.integration_name)

            # set span.kind to the type of request being performed
            s.set_tag_str(SPAN_KIND, SPAN_CLIENT)

            s.set_tags(pin.tags)
            s.set_tags(extra_tags)

            return method(*args, **kwargs)

    def cursor(self, *args, **kwargs):
        cursor = self.__wrapped__.cursor(*args, **kwargs)
        pin = Pin.get_from(self)
        if not pin:
            return cursor
        return self._self_cursor_cls(cursor, pin, self._self_config)

    def commit(self, *args, **kwargs):
        span_name = "{}.{}".format(self._self_datadog_name, "commit")
        return self._trace_method(self.__wrapped__.commit, span_name, {}, *args, **kwargs)

    def rollback(self, *args, **kwargs):
        span_name = "{}.{}".format(self._self_datadog_name, "rollback")
        return self._trace_method(self.__wrapped__.rollback, span_name, {}, *args, **kwargs)


def _get_vendor(conn):
    """Return the vendor (e.g postgres, mysql) of the given
    database.
    """
    try:
        name = _get_module_name(conn)
    except Exception:
        log.debug("couldn't parse module name", exc_info=True)
        name = "sql"
    return sql.normalize_vendor(name)


def _get_module_name(conn):
    return conn.__class__.__module__.split(".")[0]


def _inject_dbm_comment_in_query_arg(dbspan, args, kwargs):
    # type: (...) -> Tuple[Tuple[Any, ...], Dict[str, Any]]
    dbm_comment = _database_monitoring._get_dbm_comment(dbspan)
    if dbm_comment is None:
        return args, kwargs

    # if dbm comment is not None prepend it to the query
    query = get_argument_value(args, kwargs, 0, "query")
    query_with_dbm_tags = dbm_comment + query
    # replace the original query with query_with_dbm_tags
    args, kwargs = set_argument_value(args, kwargs, 0, "query", query_with_dbm_tags)
    return args, kwargs
