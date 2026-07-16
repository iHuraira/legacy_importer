"""Import complete legacy AMRFinderPlus TSV rows into existing tasks."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from .db import bulk_upsert, transaction
from .ids import result_id


TABLE_NAME = "amrfinderplus"
PRIMARY_KEY = "amrfinderplus_id"
TASK_TOOL_NAME = "amrfinder"

HEADER_TO_COLUMN = {
    "Name": "name",
    "Protein identifier": "protein_identifier",
    "Contig id": "contig_id",
    "Start": "start_position",
    "Stop": "stop_position",
    "Strand": "strand",
    "Gene symbol": "gene_symbol",
    "Sequence name": "sequence_name",
    "Scope": "scope",
    "Element type": "element_type",
    "Element subtype": "element_subtype",
    "Class": "class",
    "Subclass": "subclass",
    "Method": "method",
    "Target length": "target_length",
    "Reference sequence length": "reference_sequence_length",
    "% Coverage of reference sequence": "reference_coverage_percent",
    "% Identity to reference sequence": "reference_identity_percent",
    "Alignment length": "alignment_length",
    "Accession of closest sequence": "closest_sequence_accession",
    "Name of closest sequence": "closest_sequence_name",
    "HMM id": "hmm_id",
    "HMM description": "hmm_description",
}

INTEGER_COLUMNS = {
    "start_position",
    "stop_position",
    "target_length",
    "reference_sequence_length",
    "alignment_length",
}
DECIMAL_COLUMNS = {
    "reference_coverage_percent",
    "reference_identity_percent",
}


def _optional_text(value: str | None) -> str | None:
    normalized = (value or "").strip()
    return normalized or None


def _optional_int(value: str | None, header: str, row_number: int) -> int | None:
    normalized = _optional_text(value)
    if normalized is None:
        return None
    try:
        return int(normalized)
    except ValueError as exc:
        raise ValueError(
            f"Invalid integer in {header!r} at TSV row {row_number}: {normalized!r}"
        ) from exc


def _optional_float(value: str | None, header: str, row_number: int) -> float | None:
    normalized = _optional_text(value)
    if normalized is None:
        return None
    try:
        return float(normalized)
    except ValueError as exc:
        raise ValueError(
            f"Invalid number in {header!r} at TSV row {row_number}: {normalized!r}"
        ) from exc


def parse_amrfinderplus_tsv(path: Path) -> list[dict[str, Any]]:
    """Parse every supported AMRFinderPlus report column without data loss."""

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        missing = set(HEADER_TO_COLUMN) - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                f"{path} is missing AMRFinderPlus columns: "
                f"{', '.join(sorted(missing))}"
            )
        parsed: list[dict[str, Any]] = []
        for row_number, source in enumerate(reader, 2):
            record: dict[str, Any] = {}
            for header, column in HEADER_TO_COLUMN.items():
                if column in INTEGER_COLUMNS:
                    value = _optional_int(source.get(header), header, row_number)
                elif column in DECIMAL_COLUMNS:
                    value = _optional_float(source.get(header), header, row_number)
                else:
                    value = _optional_text(source.get(header))
                record[column] = value
            parsed.append(record)
    return parsed


def resolve_existing_amrfinder_task(connection, sample_id: UUID) -> UUID:
    """Return the single existing legacy AMRFinder task for a sample."""

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT task_id
            FROM tasks
            WHERE sample_id = %s
              AND LOWER(tool_name) = %s
            ORDER BY task_id
            """,
            (str(sample_id), TASK_TOOL_NAME),
        )
        matches = [row[0] for row in cursor.fetchall()]
    if not matches:
        raise RuntimeError(
            f"No existing {TASK_TOOL_NAME} task found for sample_id={sample_id}."
        )
    if len(matches) > 1:
        raise RuntimeError(
            f"Multiple existing {TASK_TOOL_NAME} tasks found for "
            f"sample_id={sample_id}: {matches}"
        )
    return UUID(str(matches[0]))


def build_amrfinderplus_rows(
    parsed: list[dict[str, Any]],
    sample_id: UUID,
    task_id: UUID,
    source_path: Path,
    *,
    created_at: datetime | None = None,
) -> list[dict[str, Any]]:
    """Add deterministic identifiers and database relationships to parsed hits."""

    timestamp = created_at or datetime.now(timezone.utc)
    enriched = []
    for index, values in enumerate(parsed):
        natural_key = "|".join(
            str(values.get(column) or "")
            for column in (
                "protein_identifier",
                "contig_id",
                "start_position",
                "stop_position",
                "method",
                "gene_symbol",
            )
        )
        row = dict(values)
        row[PRIMARY_KEY] = result_id(
            TABLE_NAME,
            sample_id,
            task_id,
            f"{source_path.name}:{natural_key}:{index}",
        )
        row["task_id"] = task_id
        row["sample_id"] = sample_id
        row["created_at"] = timestamp
        enriched.append(row)
    return enriched


def import_amrfinderplus_report(
    connection,
    report_path: Path,
    sample_id: UUID,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Validate, link, and optionally upsert one complete report."""

    parsed = parse_amrfinderplus_tsv(report_path)
    task_id = resolve_existing_amrfinder_task(connection, sample_id)
    rows = build_amrfinderplus_rows(parsed, sample_id, task_id, report_path)
    if not dry_run:
        with transaction(connection):
            bulk_upsert(connection, TABLE_NAME, rows, [PRIMARY_KEY])
    return {
        "task_id": task_id,
        "report": str(report_path),
        "rows": len(rows),
        "status": "validated" if dry_run else "imported",
    }
