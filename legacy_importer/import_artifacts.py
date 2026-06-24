"""Create synthetic tasks and upload/register tool artifact archives."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from .archive import create_archive, sha256_file
from .config import ImportConfig
from .db import upsert
from .gcs import upload_file
from .ids import artifact_id, task_id


def task_row(
    config: ImportConfig,
    run_id: UUID,
    sample_id: UUID,
    organization_id: UUID,
    tool_name: str,
    now: datetime,
) -> dict[str, Any]:
    """Build one deterministic synthetic task per sample and tool."""

    identifier = task_id(sample_id, tool_name)
    return {
        "task_id": identifier,
        "task_accession": f"legacy-{tool_name}-{sample_id}",
        "run_id": run_id,
        "sample_id": sample_id,
        "organization_id": organization_id,
        "tool_name": tool_name,
        "tool_version": config.tasks.get("tool_version", "legacy"),
        "state": config.tasks.get("state", "COMPLETED"),
        "source": config.source,
        "is_synthetic": True,
        "started_at": now,
        "ended_at": now,
    }


def import_artifact(
    connection,
    gcs_client,
    config: ImportConfig,
    sample_dir: Path,
    run_id: UUID,
    sample_id: UUID,
    organization_id: UUID,
    tool_name: str,
    paths: list[Path],
    upload: bool = True,
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    """Create task, archive and upload outputs, then register the final artifact."""

    now = datetime.now(timezone.utc)
    task = task_row(
        config,
        run_id,
        sample_id,
        organization_id,
        tool_name,
        now,
    )
    upsert(connection, "tasks", task, ["task_id"])
    identifier = artifact_id(sample_id, tool_name, "output")
    archive = create_archive(sample_dir, paths, identifier)
    object_name = f"{run_id}/{sample_id}/{task['task_id']}.tar.gz"
    local_checksum = sha256_file(archive)
    metadata = (
        upload_file(gcs_client, config.gcp.artifact_bucket, object_name, archive)
        if upload
        else {
            "uri": f"gs://{config.gcp.artifact_bucket}/{object_name}",
            "size_bytes": archive.stat().st_size,
            "checksum": local_checksum,
        }
    )
    artifact = {
        "artifact_id": identifier,
        "task_id": task["task_id"],
        "sample_id": sample_id,
        "organization_id": organization_id,
        "artifact_name": tool_name,
        "category": config.tools[tool_name].category,
        "output_type": config.artifacts.get("output_type", "tar.gz"),
        "uri": metadata["uri"],
        "size_bytes": metadata["size_bytes"],
        "checksum": metadata["checksum"] or local_checksum,
        "metadata": {"source_files": [str(path.relative_to(sample_dir)) for path in paths]},
        "source": config.source,
    }
    # This write intentionally occurs only after archive creation and successful upload.
    upsert(connection, "artifacts", artifact, ["artifact_id"])
    return task, artifact, archive
