import psycopg2
import pytest

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
