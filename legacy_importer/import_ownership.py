"""Create deterministic organization, user, batch, job, run, and sample ownership."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import ids
from .config import ImportConfig
from .db import upsert
from .manifest import ManifestRow


def ownership_rows(row: ManifestRow, config: ImportConfig, now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """Build all ownership rows associated with one manifest sample."""

    now = now or datetime.now(timezone.utc)
    organization_code = row.organization_code
    legacy_batch_code = row.legacy_batch_code
    organization = ids.organization_id(organization_code)
    user = ids.legacy_user_id(organization_code)
    batch = ids.batch_id(organization_code, legacy_batch_code)
    job = ids.job_id(organization_code, legacy_batch_code)
    run = ids.run_id(organization_code, legacy_batch_code)
    sample = ids.sample_id(
        organization_code,
        legacy_batch_code,
        row.sample_name,
    )
    settings = config.legacy_ownership
    return {
        "organizations": {
            "organization_id": organization,
            "organization_name": (
                row.organization_name or config.organization_name(organization_code)
            ),
            "organization_code": organization_code,
            "created_at": now,
            **config.organization_auth(organization_code),
        },
        "users": {
            "user_id": user,
            "organization_id": organization,
            "email": settings["user_email_template"].format(organization_code=organization_code),
            "display_name": settings["user_display_name_template"].format(organization_code=organization_code),
            "role": "legacy_import",
        },
        "batches": {
            "batch_id": batch,
            "organization_id": organization,
            "batch_accession": f"legacy-{organization_code}-{legacy_batch_code}",
            "user_id": user,
            "batch_name": settings["batch_name_template"].format(
                organization_code=organization_code,
                legacy_batch_code=legacy_batch_code,
            ),
            "status": "imported",
            "finished_at": now,
        },
        "jobs": {
            "job_id": job,
            "user_id": user,
            "batch_id": batch,
            "job_type": settings.get("job_type", "legacy_import"),
            "status": "imported",
            "attempt": 1,
        },
        "runs": {
            "run_id": run,
            "run_accession": f"legacy-{organization_code}-{legacy_batch_code}",
            "batch_id": batch,
            "job_id": job,
            "status": settings.get("run_status", "imported"),
        },
        "samples": {
            "sample_id": sample,
            "organization_id": organization,
            "user_id": user,
            "sample_accession": row.sample_name,
            "batch_id": batch,
            "sample_name": row.sample_name,
            "status": config.samples.status,
            "visibility": row.visibility,
            "source": config.source,
            "source_metadata": row.source_metadata(),
            "created_at": now,
        },
    }


def _reuse_existing_organization(connection, rows: dict[str, dict[str, Any]]) -> None:
    """Reuse an organization selected by its stable organization_code."""

    code = rows["organizations"]["organization_code"]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT organization_id FROM organizations WHERE organization_code = %s",
            (code,),
        )
        existing = cursor.fetchone()
    if not existing:
        return
    organization_id = existing[0]
    rows["organizations"]["organization_id"] = organization_id
    rows["users"]["organization_id"] = organization_id
    rows["batches"]["organization_id"] = organization_id
    rows["samples"]["organization_id"] = organization_id


def import_ownership(connection, rows: dict[str, dict[str, Any]]) -> None:
    """Reuse organization_code and upsert ownership in dependency order."""

    _reuse_existing_organization(connection, rows)
    primary_keys = {
        "organizations": "organization_id", "users": "user_id", "batches": "batch_id",
        "jobs": "job_id", "runs": "run_id", "samples": "sample_id",
    }
    for table in ("organizations", "users", "batches", "jobs", "runs", "samples"):
        upsert(connection, table, rows[table], [primary_keys[table]])
