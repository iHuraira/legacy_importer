import psycopg2
import pytest
import logging

import legacy_importer.db as db
from legacy_importer.db import (
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
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def execute(self, query):
            calls.append(("execute", query))

    class Connection:
        def __init__(self, notices):
            self.notices = notices
            self.autocommit = False

        def cursor(self):
            return Cursor()

    connections = [
        Connection([
            'WARNING: database "db" has a collation version mismatch\n',
            "WARNING: unrelated warning\n",
        ]),
        Connection([]),
    ]

    def fake_connect(_uri, **kwargs):
        calls.append(("connect", kwargs))
        return connections.pop(0)

    monkeypatch.setattr(db.psycopg2, "connect", fake_connect)
    config = type("Config", (), {"database_uri": lambda self: "postgresql://db"})()
    caplog.set_level(logging.WARNING)

    first = db.connect(config)
    second = db.connect(config)

    assert first.notices == ["WARNING: unrelated warning\n"]
    assert calls[0] == ("connect", {})
    assert calls[1] == (
        "connect",
        {"options": "-c client_min_messages=error"},
    )
    assert ("execute", "SET client_min_messages = warning") in calls
    messages = [
        record.getMessage()
        for record in caplog.records
        if "collation version mismatch" in record.getMessage()
    ]
    assert len(messages) == 1
