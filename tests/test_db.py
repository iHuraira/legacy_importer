import psycopg2
import pytest
import logging

import legacy_importer.db as db
from legacy_importer.db import (
    bulk_upsert,
    cached_table_columns,
    is_transient_connection_error,
    safe_close,
    safe_rollback,
    transaction,
)


class ClosedConnection:
    closed = 1

    def rollback(self):
        raise AssertionError("rollback must not be called")

    def close(self):
        raise AssertionError("close must not be called")


def test_safe_cleanup_ignores_already_closed_connection():
    connection = ClosedConnection()

    assert safe_rollback(connection) is False
    safe_close(connection)


def test_transaction_preserves_original_error_when_connection_closes():
    connection = ClosedConnection()
    original = psycopg2.InterfaceError("connection already closed")

    with pytest.raises(psycopg2.InterfaceError) as raised:
        with transaction(connection):
            raise original

    assert raised.value is original


def test_connection_errors_are_classified_as_transient():
    assert is_transient_connection_error(
        psycopg2.InterfaceError("connection already closed")
    )
    assert is_transient_connection_error(
        psycopg2.OperationalError("server closed the connection unexpectedly")
    )
    assert not is_transient_connection_error(ValueError("invalid CSV"))


def test_collation_warning_is_reported_once_and_suppressed_on_later_connects(
    monkeypatch,
    caplog,
):
    db._collation_mismatch_detected = False
    db._collation_warning_reported = False
    calls = []

    class Cursor:
        def __init__(self, result):
            self.result = result

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def execute(self, query):
            calls.append(("execute", query))

        def fetchone(self):
            return self.result

    class Connection:
        def __init__(self, result):
            self.result = result
            self.autocommit = False

        def cursor(self):
            return Cursor(self.result)

    connections = [
        Connection(("2.36", "2.43")),
        Connection(("2.36", "2.43")),
    ]

    def fake_connect(_uri, **kwargs):
        calls.append(("connect", kwargs))
        return connections.pop(0)

    monkeypatch.setattr(db.psycopg2, "connect", fake_connect)
    config = type("Config", (), {"database_uri": lambda self: "postgresql://db"})()
    caplog.set_level(logging.WARNING)

    db.connect(config)
    db.connect(config)

    assert calls[0] == (
        "connect",
        {"options": "-c client_min_messages=error"},
    )
    assert calls[1] == (
        "execute",
        """
                    SELECT datcollversion,
                           pg_database_collation_actual_version(oid)
                    FROM pg_database
                    WHERE datname = current_database()
                    """,
    )
    assert ("execute", "SET client_min_messages = warning") in calls
    assert [
        call for call in calls
        if call == ("connect", {"options": "-c client_min_messages=error"})
    ] == [
        ("connect", {"options": "-c client_min_messages=error"}),
        ("connect", {"options": "-c client_min_messages=error"}),
    ]
    messages = [
        record.getMessage()
        for record in caplog.records
        if "collation version mismatch" in record.getMessage()
    ]
    assert len(messages) == 1


def test_table_columns_are_cached_per_connection(monkeypatch):
    db._table_columns_cache.clear()
    connection = object()
    calls = []

    monkeypatch.setattr(
        db,
        "table_columns",
        lambda _connection, table: calls.append(table) or {"id", "value"},
    )

    assert cached_table_columns(connection, "results") == {"id", "value"}
    assert cached_table_columns(connection, "results") == {"id", "value"}
    assert calls == ["results"]


def test_bulk_upsert_uses_one_schema_lookup_and_bounded_values_call(monkeypatch):
    db._table_columns_cache.clear()
    captured = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

    class Connection:
        def cursor(self):
            return Cursor()

    connection = Connection()
    schema_calls = []
    monkeypatch.setattr(
        db,
        "table_columns",
        lambda _connection, table: (
            schema_calls.append(table) or {"result_id", "sample_id", "value"}
        ),
    )
    monkeypatch.setattr(
        db,
        "execute_values",
        lambda _cursor, _query, values, page_size: captured.append(
            (values, page_size)
        ),
    )

    bulk_upsert(
        connection,
        "results",
        [
            {"result_id": "1", "sample_id": "S1", "value": 10},
            {"result_id": "2", "sample_id": "S1", "value": 20},
        ],
        ["result_id"],
        page_size=1000,
    )

    assert schema_calls == ["results"]
    assert len(captured) == 1
    assert captured[0][0] == [("1", "S1", 10), ("2", "S1", 20)]
    assert captured[0][1] == 1000
