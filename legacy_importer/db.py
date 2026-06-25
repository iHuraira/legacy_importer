"""PostgreSQL connection, transaction, and idempotent upsert utilities."""

from __future__ import annotations

import json
import logging
import threading
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Iterator
from uuid import UUID

import psycopg2
from psycopg2 import sql
from psycopg2.extras import Json, execute_values

from .config import ImportConfig
from .schema import inspect_schema, table_columns
from .timing import PhaseTimer

LOG = logging.getLogger(__name__)
_collation_mismatch_detected = False
_collation_warning_reported = False
_notice_lock = threading.Lock()
_table_columns_cache: dict[tuple[int, str], set[str]] = {}


def _check_collation_version(connection) -> None:
    """Detect a database collation mismatch without emitting startup warnings."""

    global _collation_mismatch_detected, _collation_warning_reported
    previous_autocommit = connection.autocommit
    mismatch = False
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
            try:
                cursor.execute(
                    """
                    SELECT datcollversion,
                           pg_database_collation_actual_version(oid)
                    FROM pg_database
                    WHERE datname = current_database()
                    """
                )
                stored, actual = cursor.fetchone()
                mismatch = bool(stored and actual and stored != actual)
            except psycopg2.Error:
                # Older PostgreSQL versions may not expose the helper function.
                mismatch = False
            finally:
                cursor.execute("SET client_min_messages = warning")
    finally:
        connection.autocommit = previous_autocommit
    if mismatch:
        with _notice_lock:
            _collation_mismatch_detected = True
            if not _collation_warning_reported:
                LOG.warning(
                    "PostgreSQL collation version mismatch warning detected; "
                    "suppressing repeated occurrences during import. Ask a DB "
                    "admin to refresh the collation version if appropriate."
                )
                _collation_warning_reported = True


def connect(config: ImportConfig):
    """Connect using configured environment variable names without logging secrets."""

    connection = psycopg2.connect(
        config.database_uri(),
        options="-c client_min_messages=error",
    )
    _check_collation_version(connection)
    return connection


def safe_rollback(connection) -> bool:
    """Roll back when possible without masking the original database error."""

    if connection is None or getattr(connection, "closed", 1):
        return False
    try:
        connection.rollback()
        return True
    except (psycopg2.Error, AttributeError):
        return False


def safe_close(connection) -> None:
    """Close a connection without failing cleanup on a dead session."""

    if connection is None:
        return
    connection_id = id(connection)
    for key in [key for key in _table_columns_cache if key[0] == connection_id]:
        _table_columns_cache.pop(key, None)
    if getattr(connection, "closed", 1):
        return
    try:
        connection.close()
    except (psycopg2.Error, AttributeError):
        pass


def is_transient_connection_error(exc: BaseException) -> bool:
    """Identify PostgreSQL connection failures that are safe to retry."""

    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, (psycopg2.InterfaceError, psycopg2.OperationalError)):
            return True
        if isinstance(current, psycopg2.DatabaseError):
            code = getattr(current, "pgcode", None)
            if code and (code.startswith("08") or code in {"57P01", "57P02", "57P03"}):
                return True
        current = current.__cause__ or current.__context__
    return False


@contextmanager
def transaction(
    connection,
    timings: PhaseTimer | None = None,
) -> Iterator[Any]:
    """Commit a unit of import work or roll it back on any exception."""

    try:
        yield connection
        if timings:
            with timings.measure("database_write"):
                connection.commit()
        else:
            connection.commit()
    except BaseException:
        safe_rollback(connection)
        raise


def _adapt(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return Json(value)
    if isinstance(value, UUID):
        return str(value)
    return value


def cached_table_columns(connection, table: str) -> set[str]:
    """Read table columns once per short-lived PostgreSQL connection."""

    key = (id(connection), table)
    if key not in _table_columns_cache:
        _table_columns_cache[key] = table_columns(connection, table)
    return _table_columns_cache[key]


def upsert(
    connection,
    table: str,
    row: dict[str, Any],
    conflict_columns: list[str],
) -> None:
    """Upsert only columns that actually exist in the target deployment."""

    available = cached_table_columns(connection, table)
    filtered = {key: value for key, value in row.items() if key in available}
    missing_conflicts = set(conflict_columns) - set(filtered)
    if missing_conflicts:
        raise RuntimeError(f"{table} lacks conflict columns: {sorted(missing_conflicts)}")
    columns = list(filtered)
    updates = [column for column in columns if column not in conflict_columns]
    query = sql.SQL("INSERT INTO {table} ({columns}) VALUES ({values}) ON CONFLICT ({conflicts}) ").format(
        table=sql.Identifier(table),
        columns=sql.SQL(", ").join(map(sql.Identifier, columns)),
        values=sql.SQL(", ").join(sql.Placeholder() for _ in columns),
        conflicts=sql.SQL(", ").join(map(sql.Identifier, conflict_columns)),
    )
    if updates:
        query += sql.SQL("DO UPDATE SET ") + sql.SQL(", ").join(
            sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(column), sql.Identifier(column))
            for column in updates
        )
    else:
        query += sql.SQL("DO NOTHING")
    with connection.cursor() as cursor:
        cursor.execute(query, [_adapt(filtered[column]) for column in columns])


def bulk_upsert(
    connection,
    table: str,
    rows: list[dict[str, Any]],
    conflict_columns: list[str],
    page_size: int = 1000,
) -> None:
    """Bulk upsert rows, grouping differing record shapes safely."""

    if not rows:
        return
    available = cached_table_columns(connection, table)
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for row in rows:
        filtered = {key: value for key, value in row.items() if key in available}
        missing_conflicts = set(conflict_columns) - set(filtered)
        if missing_conflicts:
            raise RuntimeError(
                f"{table} lacks conflict columns: {sorted(missing_conflicts)}"
            )
        groups.setdefault(tuple(filtered), []).append(filtered)

    for columns, group in groups.items():
        updates = [column for column in columns if column not in conflict_columns]
        query = sql.SQL(
            "INSERT INTO {table} ({columns}) VALUES %s ON CONFLICT ({conflicts}) "
        ).format(
            table=sql.Identifier(table),
            columns=sql.SQL(", ").join(map(sql.Identifier, columns)),
            conflicts=sql.SQL(", ").join(map(sql.Identifier, conflict_columns)),
        )
        if updates:
            query += sql.SQL("DO UPDATE SET ") + sql.SQL(", ").join(
                sql.SQL("{} = EXCLUDED.{}").format(
                    sql.Identifier(column),
                    sql.Identifier(column),
                )
                for column in updates
            )
        else:
            query += sql.SQL("DO NOTHING")
        values = [
            tuple(_adapt(row[column]) for column in columns)
            for row in group
        ]
        with connection.cursor() as cursor:
            execute_values(cursor, query, values, page_size=page_size)


def fetch_rows(connection, table: str, sample_id: UUID) -> list[dict[str, Any]]:
    """Fetch sample-scoped result rows as dictionaries for QC and verification."""

    if "sample_id" not in cached_table_columns(connection, table):
        return []
    query = sql.SQL("SELECT * FROM {} WHERE sample_id = %s").format(sql.Identifier(table))
    with connection.cursor() as cursor:
        cursor.execute(query, (str(sample_id),))
        names = [description.name for description in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]


def test_database(config: ImportConfig, write_test: bool = False) -> dict[str, Any]:
    """Run connectivity, schema, and optional rolled-back write checks."""

    connection = connect(config)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database(), current_user, now()")
            database, user, timestamp = cursor.fetchone()
        schema_report = inspect_schema(connection)
        write_ok = None
        if write_test:
            with connection.cursor() as cursor:
                cursor.execute("CREATE TEMP TABLE legacy_import_write_test (id integer)")
                cursor.execute("INSERT INTO legacy_import_write_test VALUES (1)")
            safe_rollback(connection)
            write_ok = True
        return {
            "database": database,
            "user": user,
            "server_time": timestamp.isoformat(),
            "schema": schema_report,
            "write_test": write_ok,
        }
    finally:
        safe_close(connection)
