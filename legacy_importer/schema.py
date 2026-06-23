"""Required database schema definitions and validation helpers."""

from __future__ import annotations

CORE_SCHEMA: dict[str, set[str]] = {
    "organizations": {"organization_id", "organization_name", "organization_code", "created_at"},
    "users": {"user_id", "organization_id", "email", "display_name", "role"},
    "batches": {"batch_id", "organization_id", "batch_accession", "user_id", "batch_name", "status"},
    "jobs": {"job_id", "user_id", "batch_id", "job_type", "status", "attempt"},
    "runs": {"run_id", "run_accession", "batch_id", "job_id", "status"},
    "samples": {
        "sample_id", "organization_id", "user_id", "sample_accession", "batch_id",
        "sample_name", "status", "visibility", "source", "source_metadata",
    },
    "reads": {
        "read_id", "sample_id", "read_type", "original_filename", "uri",
        "size_bytes", "checksum", "status",
    },
    "tasks": {
        "task_id", "task_accession", "run_id", "sample_id", "tool_name",
        "tool_version", "state", "source", "is_synthetic",
    },
    "artifacts": {
        "artifact_id", "task_id", "sample_id", "artifact_name", "category",
        "output_type", "uri", "size_bytes", "checksum", "metadata", "source",
    },
    "mash_master": {
        "mash_master_id", "task_id", "batch_id", "artifact_id", "version",
        "batch_sketch_count", "cumulative_sketch_count", "scope",
        "organization_id", "is_active", "created_at",
    },
    "mash_master_samples": {
        "mash_master_id", "sample_id", "organization_id", "added_at",
    },
    "ska_master": {
        "ska_master_id", "task_id", "batch_id", "artifact_id", "version",
        "sample_count", "created_at",
    },
    "ska_distance": {
        "ska_distance_id", "ska_master_id", "sample1_id", "sample2_id",
        "snps", "category", "created_at",
    },
}

RESULT_TABLES = {"fastqc", "mlst", "quast", "bbmap", "bracken", "amrfinder", "qc1", "qc2"}


def inspect_schema(connection) -> dict[str, object]:
    """Read public table columns and report missing required schema elements."""

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
            """
        )
        rows = cursor.fetchall()
    actual: dict[str, set[str]] = {}
    for table, column in rows:
        actual.setdefault(table, set()).add(column)
    missing_tables = sorted(table for table in CORE_SCHEMA if table not in actual)
    missing_columns = {
        table: sorted(columns - actual.get(table, set()))
        for table, columns in CORE_SCHEMA.items()
        if columns - actual.get(table, set())
    }
    missing_results = sorted(
        table for table in RESULT_TABLES if table not in actual
    )
    has_annotation = "bakta_annotations" in actual or any(
        name.startswith("prokka") for name in actual
    )
    if not has_annotation:
        missing_results.append("bakta_annotations or prokka-related table")
    return {
        "ok": not missing_tables and not missing_columns and not missing_results,
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "missing_result_tables": missing_results,
        "tables": {table: sorted(columns) for table, columns in actual.items()},
    }


def table_columns(connection, table_name: str) -> set[str]:
    """Return columns for a safe, parameterized public table lookup."""

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            """,
            (table_name,),
        )
        return {row[0] for row in cursor.fetchall()}
