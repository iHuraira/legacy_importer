"""Verify imported database records and their referenced GCS objects."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg2 import sql

from .config import ImportConfig
from .gcs import client_from_config, object_exists
from .db import safe_rollback
from .schema import table_columns


def _count(connection, table: str, column: str, value: UUID) -> int:
    """Count records using quoted identifiers."""

    query = sql.SQL("SELECT count(*) FROM {} WHERE {} = %s").format(
        sql.Identifier(table), sql.Identifier(column)
    )
    with connection.cursor() as cursor:
        cursor.execute(query, (str(value),))
        return cursor.fetchone()[0]


def _uris(connection, table: str, sample_id: UUID) -> list[str]:
    """Read non-empty URIs for one sample."""

    if not {"sample_id", "uri"} <= table_columns(connection, table):
        return []
    query = sql.SQL("SELECT uri FROM {} WHERE sample_id = %s AND uri IS NOT NULL").format(
        sql.Identifier(table)
    )
    with connection.cursor() as cursor:
        cursor.execute(query, (str(sample_id),))
        return [row[0] for row in cursor.fetchall()]


def verify_sample(connection, config: ImportConfig, identifiers: dict[str, Any]) -> dict[str, Any]:
    """Check ownership, reads, tasks, artifacts, results, QC, and GCS objects."""

    sample_id = identifiers["sample_id"]
    checks = {
        "organization": 0,
        "sample": _count(connection, "samples", "sample_id", sample_id),
        "reads": _count(connection, "reads", "sample_id", sample_id),
        "tasks": _count(connection, "tasks", "sample_id", sample_id),
        "artifacts": _count(connection, "artifacts", "sample_id", sample_id),
        "qc1": _count(connection, "qc1", "sample_id", sample_id),
        "qc2": _count(connection, "qc2", "sample_id", sample_id),
    }
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM organizations WHERE organization_code = %s",
            (identifiers["organization_code"],),
        )
        checks["organization"] = cursor.fetchone()[0]
    result_counts = {}
    for tool, tool_config in config.tools.items():
        if not tool_config.extractor_enabled:
            continue
        table = tool_config.result_table or ("bakta_annotations" if tool == "prokka" else tool)
        try:
            result_counts[table] = _count(connection, table, "sample_id", sample_id)
        except Exception:
            safe_rollback(connection)
            result_counts[table] = None
    gcs_client = client_from_config(config)
    uris = _uris(connection, "reads", sample_id) + _uris(connection, "artifacts", sample_id)
    objects = {uri: object_exists(gcs_client, uri) for uri in uris}
    return {
        "ok": checks["organization"] == 1 and checks["sample"] == 1 and all(objects.values()),
        "checks": checks,
        "results": result_counts,
        "gcs_objects": objects,
    }
