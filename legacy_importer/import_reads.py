"""Upload and register raw paired FASTQ reads."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

from .archive import sha256_file
from .config import ImportConfig
from .db import upsert
from .gcs import upload_file
from .ids import read_id
from .manifest import ManifestRow
from .timing import PhaseTimer


def raw_read_object_name(config: ImportConfig, row: ManifestRow, filename: str) -> str:
    """Build the stable GCS object name for a raw read."""

    prefix = config.gcp.raw_reads_prefix.strip("/")
    return (
        f"{prefix}/{row.organization_code}/{row.legacy_batch_code}/"
        f"{row.sample_name}/{filename}"
    )


def import_reads(
    connection,
    gcs_client,
    config: ImportConfig,
    manifest_row: ManifestRow,
    sample_id: UUID,
    reads: dict[str, Path],
    upload: bool = True,
    timings: PhaseTimer | None = None,
) -> dict[str, dict[str, Any]]:
    """Upload raw reads, then idempotently register each row."""

    imported: dict[str, dict[str, Any]] = {}
    for read_type, path in reads.items():
        identifier = read_id(sample_id, read_type, path.name)
        object_name = raw_read_object_name(config, manifest_row, path.name)
        uri = f"gs://{config.gcp.raw_reads_bucket}/{object_name}"
        if timings:
            with timings.measure("checksum"):
                local_checksum = sha256_file(path)
        else:
            local_checksum = sha256_file(path)
        if upload:
            if timings:
                with timings.measure("gcs_upload"):
                    metadata = upload_file(
                        gcs_client,
                        config.gcp.raw_reads_bucket,
                        object_name,
                        path,
                    )
            else:
                metadata = upload_file(
                    gcs_client,
                    config.gcp.raw_reads_bucket,
                    object_name,
                    path,
                )
        else:
            metadata = {
                "uri": uri,
                "size_bytes": path.stat().st_size,
                "checksum": local_checksum,
            }
        record = {
            "read_id": identifier,
            "sample_id": sample_id,
            "read_type": read_type,
            "original_filename": path.name,
            "uri": metadata["uri"],
            "size_bytes": metadata["size_bytes"],
            "checksum": metadata["checksum"] or local_checksum,
            "status": config.reads.get("status", "imported"),
        }
        if timings:
            with timings.measure("database_write"):
                upsert(connection, "reads", record, ["read_id"])
        else:
            upsert(connection, "reads", record, ["read_id"])
        imported[read_type] = record
    return imported
