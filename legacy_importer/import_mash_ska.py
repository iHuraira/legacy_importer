"""Import existing legacy MASH master and SKA distance data."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator
from uuid import UUID

from psycopg2.extras import execute_values

from . import ids
from .archive import create_archive, sha256_file
from .config import ImportConfig
from .db import transaction, upsert
from .gcs import client_from_config, object_exists, upload_file
from .manifest import read_manifest
from .schema import table_columns

ProgressCallback = Callable[[str, int, int], None]


def _progress(
    callback: ProgressCallback | None,
    label: str,
    current: int,
    total: int,
) -> None:
    """Send a progress update when a caller requested one."""

    if callback:
        callback(label, current, total)


@dataclass(frozen=True)
class SampleRecord:
    """Store the database identifiers needed by MASH and SKA imports."""

    sample_id: UUID
    organization_id: UUID
    sample_name: str
    sample_accession: str


class MissingSamplesError(ValueError):
    """Report sample names that do not map to one database sample."""

    def __init__(self, missing: Iterable[str], ambiguous: Iterable[str] = ()):
        self.missing = sorted(set(missing))
        self.ambiguous = sorted(set(ambiguous))
        parts = []
        if self.missing:
            parts.append(f"missing={len(self.missing)}")
        if self.ambiguous:
            parts.append(f"ambiguous={len(self.ambiguous)}")
        super().__init__("Sample mapping failed: " + ", ".join(parts))

    def report(self) -> dict[str, Any]:
        """Return a JSON-safe missing sample report."""

        return {
            "ok": False,
            "missing_count": len(self.missing),
            "missing_samples": self.missing,
            "ambiguous_count": len(self.ambiguous),
            "ambiguous_samples": self.ambiguous,
        }


GLOBAL_BATCH_ACCESSION = "LEGACY-GLOBAL-BATCH"
GLOBAL_RUN_ACCESSION = "LEGACY-GLOBAL-RUN"


def global_context_rows(
    config: ImportConfig,
    now: datetime,
    batch_id: UUID | None = None,
    run_id: UUID | None = None,
) -> dict[str, dict[str, Any]]:
    """Build the deterministic synthetic global ownership rows."""

    organization_id = ids.global_organization_id()
    user_id = ids.global_user_id()
    batch_id = batch_id or ids.global_batch_id()
    job_id = ids.global_job_id(batch_id)
    run_id = run_id or ids.global_run_id(batch_id)
    return {
        "organizations": {
            "organization_id": organization_id,
            "organization_name": "Legacy Global Import",
            "organization_code": "legacy-global",
            "created_at": now,
        },
        "users": {
            "user_id": user_id,
            "organization_id": organization_id,
            "email": "legacy-global-import@local",
            "display_name": "Legacy Global Import",
            "role": "legacy_import",
        },
        "batches": {
            "batch_id": batch_id,
            "organization_id": organization_id,
            "batch_accession": GLOBAL_BATCH_ACCESSION,
            "user_id": user_id,
            "batch_name": "Legacy Global Import",
            "status": "imported",
            "source": config.source,
            "created_at": now,
            "finished_at": now,
        },
        "jobs": {
            "job_id": job_id,
            "user_id": user_id,
            "batch_id": batch_id,
            "job_type": "legacy_import",
            "status": "imported",
            "attempt": 1,
            "source": config.source,
            "created_at": now,
        },
        "runs": {
            "run_id": run_id,
            "run_accession": GLOBAL_RUN_ACCESSION,
            "batch_id": batch_id,
            "job_id": job_id,
            "status": "imported",
            "source": config.source,
            "created_at": now,
        },
    }


def ensure_global_context(
    connection,
    config: ImportConfig,
    now: datetime | None = None,
) -> tuple[UUID, UUID]:
    """Create or reuse the dedicated synthetic global batch and run."""

    now = now or datetime.now(timezone.utc)
    batch_id = None
    run_id = None
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT batch_id FROM batches WHERE batch_accession = %s",
            (GLOBAL_BATCH_ACCESSION,),
        )
        existing_batch = cursor.fetchone()
        if existing_batch:
            batch_id = UUID(str(existing_batch[0]))
        cursor.execute(
            "SELECT run_id, batch_id FROM runs WHERE run_accession = %s",
            (GLOBAL_RUN_ACCESSION,),
        )
        existing_run = cursor.fetchone()
        if existing_run:
            run_id = UUID(str(existing_run[0]))
            run_batch_id = UUID(str(existing_run[1]))
            if batch_id is not None and run_batch_id != batch_id:
                raise RuntimeError(
                    f"{GLOBAL_RUN_ACCESSION} belongs to a different batch."
                )
            batch_id = run_batch_id
    rows = global_context_rows(config, now, batch_id=batch_id, run_id=run_id)
    for table in ("organizations", "users", "batches", "jobs", "runs"):
        primary_key = next(key for key in rows[table] if key.endswith("_id"))
        upsert(connection, table, rows[table], [primary_key])
    return rows["batches"]["batch_id"], rows["runs"]["run_id"]


def lookup_global_context(connection) -> tuple[UUID, UUID] | None:
    """Look up the synthetic global batch and matching run by accession."""

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT b.batch_id, r.run_id
            FROM batches b
            JOIN runs r ON r.batch_id = b.batch_id
            WHERE b.batch_accession = %s AND r.run_accession = %s
            """,
            (GLOBAL_BATCH_ACCESSION, GLOBAL_RUN_ACCESSION),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    return UUID(str(row[0])), UUID(str(row[1]))


def batch_artifact_object_name(run_id: UUID, task_id: UUID) -> str:
    """Build the existing batch artifact storage path."""

    return f"{run_id}/batch/{task_id}.tar.gz"


def synthetic_batch_task(
    config: ImportConfig,
    run_id: UUID,
    batch_id: UUID,
    tool_name: str,
    version: int | str,
    now: datetime,
) -> dict[str, Any]:
    """Build one deterministic completed batch task."""

    task_id = ids.batch_task_id(batch_id, tool_name, version)
    return {
        "task_id": task_id,
        "task_accession": f"legacy-{tool_name}-{version}-{batch_id}",
        "run_id": run_id,
        "sample_id": None,
        "organization_id": None,
        "tool_name": tool_name,
        "tool_version": "legacy",
        "state": "COMPLETED",
        "source": config.source,
        "is_synthetic": True,
        "started_at": now,
        "ended_at": now,
    }


def _require_tables(connection, required: dict[str, set[str]]) -> None:
    """Fail before upload when required MASH/SKA table columns are missing."""

    failures = {}
    for table, columns in required.items():
        missing = columns - table_columns(connection, table)
        if missing:
            failures[table] = sorted(missing)
    if failures:
        raise RuntimeError(f"MASH/SKA schema is incomplete: {failures}")


def load_sample_records(
    connection,
    source_filter: str | None,
) -> list[SampleRecord]:
    """Load imported sample names and identifiers from the database."""

    query = """
        SELECT sample_id, organization_id, sample_name, sample_accession
        FROM samples
    """
    params: tuple[Any, ...] = ()
    if source_filter:
        query += " WHERE source = %s"
        params = (source_filter,)
    with connection.cursor() as cursor:
        cursor.execute(query, params)
        return [
            SampleRecord(
                sample_id=UUID(str(sample_id)),
                organization_id=UUID(str(organization_id)),
                sample_name=sample_name,
                sample_accession=sample_accession,
            )
            for sample_id, organization_id, sample_name, sample_accession
            in cursor.fetchall()
        ]


def build_sample_index(
    records: Iterable[SampleRecord],
) -> tuple[dict[str, SampleRecord], set[str]]:
    """Index unique sample names and accessions and flag ambiguous aliases."""

    index: dict[str, SampleRecord] = {}
    ambiguous: set[str] = set()
    for record in records:
        for alias in {record.sample_name, record.sample_accession}:
            alias = alias.strip()
            if not alias:
                continue
            existing = index.get(alias)
            if existing and existing.sample_id != record.sample_id:
                ambiguous.add(alias)
                index.pop(alias, None)
            elif alias not in ambiguous:
                index[alias] = record
    return index, ambiguous


def apply_ska_sample_overrides(
    config: ImportConfig,
    records: Iterable[SampleRecord],
    index: dict[str, SampleRecord],
    ambiguous: set[str],
) -> None:
    """Resolve ambiguous SKA names using explicit manifest identity overrides."""

    by_id = {record.sample_id: record for record in records}
    for alias, override in config.mash_ska.ska_sample_overrides.items():
        sample_id = ids.sample_id(
            override.organization_code,
            override.legacy_batch_code,
            override.sample_name,
        )
        record = by_id.get(sample_id)
        if record is None:
            raise ValueError(
                f"SKA sample override {alias!r} does not match an imported sample: "
                f"{override.organization_code}/{override.legacy_batch_code}/"
                f"{override.sample_name}"
            )
        index[alias] = record
        ambiguous.discard(alias)


def map_sample_names(
    names: Iterable[str],
    index: dict[str, SampleRecord],
    ambiguous: set[str] | None = None,
) -> list[SampleRecord]:
    """Map sample names to database records or raise a detailed report."""

    ambiguous = ambiguous or set()
    missing_names = []
    ambiguous_names = []
    mapped: dict[UUID, SampleRecord] = {}
    for raw_name in names:
        name = raw_name.strip()
        if name in ambiguous:
            ambiguous_names.append(name)
            continue
        record = index.get(name)
        if record is None:
            missing_names.append(name)
            continue
        mapped[record.sample_id] = record
    if missing_names or ambiguous_names:
        raise MissingSamplesError(missing_names, ambiguous_names)
    return list(mapped.values())


def mash_membership_records(
    connection,
    config: ImportConfig,
) -> list[SampleRecord]:
    """Resolve all samples represented by the legacy MASH master."""

    source = config.mash_ska.sample_source
    database_records = load_sample_records(connection, source.source_filter)
    manifest_path = (
        config.repo_path(source.manifest_csv)
        if source.manifest_csv is not None
        else None
    )
    if source.mode in {"manifest", "manifest_or_database"} and manifest_path:
        if manifest_path.is_file():
            by_id = {record.sample_id: record for record in database_records}
            mapped = []
            missing = []
            for row in read_manifest(manifest_path):
                sample_id = ids.sample_id(
                    row.organization_code,
                    row.legacy_batch_code,
                    row.sample_name,
                )
                record = by_id.get(sample_id)
                if record is None:
                    missing.append(
                        f"{row.organization_code}/{row.legacy_batch_code}/{row.sample_name}"
                    )
                else:
                    mapped.append(record)
            if missing:
                raise MissingSamplesError(missing)
            return mapped
        if source.mode == "manifest":
            raise FileNotFoundError(manifest_path)
    return database_records


def insert_mash_master_samples(
    connection,
    mash_master_id: UUID,
    samples: Iterable[SampleRecord],
    added_at: datetime,
    page_size: int = 5000,
) -> int:
    """Bulk upsert the sample membership of one MASH master."""

    rows = [
        (
            str(mash_master_id),
            str(sample.sample_id),
            str(sample.organization_id),
            added_at,
        )
        for sample in samples
    ]
    if not rows:
        return 0
    with connection.cursor() as cursor:
        execute_values(
            cursor,
            """
            INSERT INTO mash_master_samples
                (mash_master_id, sample_id, organization_id, added_at)
            VALUES %s
            ON CONFLICT (mash_master_id, sample_id) DO UPDATE SET
                organization_id = EXCLUDED.organization_id,
                added_at = EXCLUDED.added_at
            """,
            rows,
            page_size=page_size,
        )
    return len(rows)


def import_mash_master(
    connection,
    config: ImportConfig,
    gcs_client=None,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Archive, upload, and register the existing global MASH master."""

    if not config.mash_ska.enabled:
        raise RuntimeError("mash_ska is disabled")
    mash_path = config.repo_path(config.mash_ska.mash_master_file)
    if not mash_path.is_file():
        raise FileNotFoundError(mash_path)
    now = datetime.now(timezone.utc)
    _require_tables(
        connection,
        {
            "organizations": {"organization_id", "organization_code"},
            "users": {"user_id", "organization_id"},
            "batches": {"batch_id", "batch_accession", "user_id", "finished_at"},
            "jobs": {"job_id", "batch_id", "user_id"},
            "runs": {"run_id", "run_accession", "batch_id", "job_id"},
            "tasks": {"task_id", "run_id", "tool_name", "organization_id"},
            "artifacts": {"artifact_id", "task_id", "uri", "organization_id"},
            "mash_master": {"mash_master_id", "task_id", "batch_id", "artifact_id"},
            "mash_master_samples": {
                "mash_master_id", "sample_id", "organization_id", "added_at"
            },
        },
    )
    with transaction(connection):
        batch_id, run_id = ensure_global_context(connection, config, now)
    task = synthetic_batch_task(
        config,
        run_id,
        batch_id,
        "mash_batch",
        config.mash_ska.mash_version,
        now,
    )
    artifact_id = ids.batch_artifact_id(task["task_id"], "mash_master")
    master_id = ids.mash_master_id(batch_id, config.mash_ska.mash_version)
    _progress(progress, "MASH: validating", 0, 5)
    samples = mash_membership_records(connection, config)
    _progress(progress, "MASH: validating", 1, 5)
    archive = create_archive(
        mash_path.parent,
        [mash_path],
        artifact_id,
        compression_level=config.artifacts.compression_level,
    )
    _progress(progress, "MASH: archived", 2, 5)
    local_checksum = sha256_file(archive)
    _progress(progress, "MASH: checksummed", 3, 5)
    object_name = batch_artifact_object_name(run_id, task["task_id"])
    client = gcs_client or client_from_config(config)
    try:
        metadata = upload_file(
            client,
            config.gcp.artifact_bucket,
            object_name,
            archive,
        )
        _progress(progress, "MASH: uploaded", 4, 5)
        with transaction(connection):
            upsert(connection, "tasks", task, ["task_id"])
            artifact = {
                "artifact_id": artifact_id,
                "task_id": task["task_id"],
                "sample_id": None,
                "organization_id": None,
                "artifact_name": "mash_master",
                "category": "distance",
                "output_type": "tar.gz",
                "uri": metadata["uri"],
                "size_bytes": metadata["size_bytes"],
                "checksum": metadata["checksum"] or local_checksum,
                "metadata": {"source_file": mash_path.name},
                "source": config.source,
            }
            upsert(connection, "artifacts", artifact, ["artifact_id"])
            sample_count = len(samples)
            master = {
                "mash_master_id": master_id,
                "task_id": task["task_id"],
                "batch_id": batch_id,
                "artifact_id": artifact_id,
                "version": config.mash_ska.mash_version,
                "batch_sketch_count": (
                    config.mash_ska.batch_sketch_count
                    if config.mash_ska.batch_sketch_count is not None
                    else sample_count
                ),
                "cumulative_sketch_count": (
                    config.mash_ska.cumulative_sketch_count
                    if config.mash_ska.cumulative_sketch_count is not None
                    else sample_count
                ),
                "scope": config.mash_ska.mash_scope,
                "organization_id": None,
                "is_active": config.mash_ska.mash_is_active,
                "created_at": now,
            }
            upsert(connection, "mash_master", master, ["mash_master_id"])
            insert_mash_master_samples(connection, master_id, samples, now)
        _progress(progress, "MASH: registered", 5, 5)
    finally:
        archive.unlink(missing_ok=True)
    return {
        "mash_master_id": master_id,
        "task_id": task["task_id"],
        "artifact_id": artifact_id,
        "artifact_uri": metadata["uri"],
        "sample_count": len(samples),
    }


def category_for_snps(config: ImportConfig, snps: int) -> str:
    """Assign an SKA category using configured SNP thresholds."""

    thresholds = config.mash_ska.ska_category_thresholds
    if snps <= thresholds.close_max_snps:
        return "close"
    if snps <= thresholds.related_max_snps:
        return "related"
    return thresholds.default_category


def _ska_reader(config: ImportConfig) -> tuple[Path, Iterator[dict[str, str]]]:
    """Open the configured SKA CSV and validate configured columns."""

    path = config.repo_path(config.mash_ska.ska_distances_csv)
    if not path.is_file():
        raise FileNotFoundError(path)
    handle = path.open("r", encoding="utf-8-sig", newline="")
    reader = csv.DictReader(handle)
    columns = config.mash_ska.ska_csv_columns
    required = {columns.sample1, columns.sample2, columns.snps}
    if columns.category:
        required.add(columns.category)
    missing = required - set(reader.fieldnames or ())
    if missing:
        handle.close()
        raise ValueError(f"SKA CSV is missing columns: {', '.join(sorted(missing))}")

    def rows() -> Iterator[dict[str, str]]:
        try:
            yield from reader
        finally:
            handle.close()

    return path, rows()


def count_ska_csv_rows(config: ImportConfig) -> int:
    """Count SKA data rows so CLI progress can show a percentage."""

    path = config.repo_path(config.mash_ska.ska_distances_csv)
    if not path.is_file():
        raise FileNotFoundError(path)
    lines = 0
    last_byte = b""
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            lines += chunk.count(b"\n")
            last_byte = chunk[-1:]
    if last_byte and last_byte != b"\n":
        lines += 1
    return max(0, lines - 1)


def ska_checkpoint_path(config: ImportConfig, master_id: UUID) -> Path:
    """Return the durable checkpoint file for one SKA master import."""

    directory = config.repo_path(config.mash_ska.checkpoint_dir)
    return directory / f"ska-{master_id}.json"


def load_ska_checkpoint(
    config: ImportConfig,
    master_id: UUID,
    csv_path: Path,
) -> int:
    """Read the last committed CSV row when the input file still matches."""

    path = ska_checkpoint_path(config, master_id)
    if not path.is_file():
        return 0
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    stat = csv_path.stat()
    expected = {
        "master_id": str(master_id),
        "csv_path": str(csv_path.resolve()),
        "csv_size": stat.st_size,
        "csv_mtime_ns": stat.st_mtime_ns,
    }
    if any(state.get(key) != value for key, value in expected.items()):
        return 0
    return max(0, int(state.get("last_committed_row", 0)))


def save_ska_checkpoint(
    config: ImportConfig,
    master_id: UUID,
    csv_path: Path,
    last_committed_row: int,
) -> Path:
    """Atomically save the last CSV row committed to PostgreSQL."""

    path = ska_checkpoint_path(config, master_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    stat = csv_path.stat()
    state = {
        "master_id": str(master_id),
        "csv_path": str(csv_path.resolve()),
        "csv_size": stat.st_size,
        "csv_mtime_ns": stat.st_mtime_ns,
        "last_committed_row": last_committed_row,
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temporary.replace(path)
    return path


def ska_csv_sample_names(
    config: ImportConfig,
    progress: ProgressCallback | None = None,
    total_rows: int | None = None,
) -> set[str]:
    """Read the unique sample names represented in the SKA CSV."""

    columns = config.mash_ska.ska_csv_columns
    _, rows = _ska_reader(config)
    names: set[str] = set()
    total = total_rows or 0
    for row_number, row in enumerate(rows, 1):
        names.add(row[columns.sample1].strip())
        names.add(row[columns.sample2].strip())
        if row_number % 10000 == 0 or row_number == total:
            _progress(progress, "SKA: mapping samples", row_number, total)
    return names


def build_ska_distance_row(
    config: ImportConfig,
    ska_master_id: UUID,
    sample1: SampleRecord,
    sample2: SampleRecord,
    raw_snps: str,
    raw_category: str | None,
    created_at: datetime,
) -> dict[str, Any]:
    """Normalize one SKA pair and build its deterministic database row."""

    snps = int(raw_snps)
    first, second = sorted(
        (sample1, sample2),
        key=lambda sample: str(sample.sample_id),
    )
    category = (raw_category or "").strip() or category_for_snps(config, snps)
    return {
        "ska_distance_id": ids.ska_distance_id(
            ska_master_id,
            first.sample_id,
            second.sample_id,
        ),
        "ska_master_id": ska_master_id,
        "sample1_id": first.sample_id,
        "sample2_id": second.sample_id,
        "snps": snps,
        "category": category,
        "created_at": created_at,
    }


def add_unique_ska_distance(
    row: dict[str, Any],
    seen: set[bytes],
    pending: list[dict[str, Any]],
) -> bool:
    """Append a distance only once for normalized reciprocal pairs."""

    key = row["ska_distance_id"].bytes
    if key in seen:
        return False
    seen.add(key)
    pending.append(row)
    return True


def _insert_ska_distance_batch(connection, rows: list[dict[str, Any]]) -> None:
    """Bulk upsert one batch of normalized SKA distances."""

    if not rows:
        return
    values = [
        (
            str(row["ska_distance_id"]),
            str(row["ska_master_id"]),
            str(row["sample1_id"]),
            str(row["sample2_id"]),
            row["snps"],
            row["category"],
            row["created_at"],
        )
        for row in rows
    ]
    with connection.cursor() as cursor:
        execute_values(
            cursor,
            """
            INSERT INTO ska_distance
                (ska_distance_id, ska_master_id, sample1_id, sample2_id,
                 snps, category, created_at)
            VALUES %s
            ON CONFLICT (ska_distance_id) DO UPDATE SET
                snps = EXCLUDED.snps,
                category = EXCLUDED.category
            """,
            values,
            page_size=len(values),
        )


def import_ska_distances(
    connection,
    config: ImportConfig,
    batch_size: int = 5000,
    progress: ProgressCallback | None = None,
    resume: bool = True,
) -> dict[str, Any]:
    """Import precomputed SKA distances without running SKA."""

    if not config.mash_ska.enabled:
        raise RuntimeError("mash_ska is disabled")
    _require_tables(
        connection,
        {
            "organizations": {"organization_id", "organization_code"},
            "users": {"user_id", "organization_id"},
            "batches": {"batch_id", "batch_accession", "user_id", "finished_at"},
            "jobs": {"job_id", "batch_id", "user_id"},
            "runs": {"run_id", "run_accession", "batch_id", "job_id"},
            "tasks": {"task_id", "run_id", "tool_name", "organization_id"},
            "ska_master": {"ska_master_id", "task_id", "batch_id", "sample_count"},
            "ska_distance": {
                "ska_distance_id", "ska_master_id", "sample1_id", "sample2_id",
                "snps", "category",
            },
        },
    )
    now = datetime.now(timezone.utc)
    with transaction(connection):
        batch_id, run_id = ensure_global_context(connection, config, now)
    _progress(progress, "SKA: counting rows", 0, 1)
    total_rows = count_ska_csv_rows(config)
    _progress(progress, "SKA: counting rows", 1, 1)
    records = load_sample_records(
        connection,
        config.mash_ska.sample_source.source_filter,
    )
    index, ambiguous = build_sample_index(records)
    apply_ska_sample_overrides(config, records, index, ambiguous)
    csv_names = ska_csv_sample_names(config, progress, total_rows)
    mapped_samples = map_sample_names(csv_names, index, ambiguous)
    task = synthetic_batch_task(
        config,
        run_id,
        batch_id,
        "ska_batch",
        config.mash_ska.ska_version,
        now,
    )
    master_id = ids.ska_master_id(batch_id, config.mash_ska.ska_version)
    csv_path = config.repo_path(config.mash_ska.ska_distances_csv)
    if not resume:
        ska_checkpoint_path(config, master_id).unlink(missing_ok=True)
    start_row = load_ska_checkpoint(config, master_id, csv_path)
    columns = config.mash_ska.ska_csv_columns
    seen: set[bytes] = set()
    imported = 0
    pending: list[dict[str, Any]] = []
    with transaction(connection):
        upsert(connection, "tasks", task, ["task_id"])
        master = {
            "ska_master_id": master_id,
            "task_id": task["task_id"],
            "batch_id": batch_id,
            "artifact_id": None,
            "version": config.mash_ska.ska_version,
            "sample_count": len(mapped_samples),
            "created_at": now,
        }
        upsert(connection, "ska_master", master, ["ska_master_id"])
    _progress(progress, "SKA: importing distances", start_row, total_rows)
    _, rows = _ska_reader(config)
    last_row = start_row
    for row_number, raw in enumerate(rows, 1):
        if row_number <= start_row:
            continue
        first = index[raw[columns.sample1].strip()]
        second = index[raw[columns.sample2].strip()]
        distance = build_ska_distance_row(
            config,
            master_id,
            first,
            second,
            raw[columns.snps],
            raw.get(columns.category) if columns.category else None,
            now,
        )
        if row_number % 10000 == 0 or row_number == total_rows:
            _progress(progress, "SKA: importing distances", row_number, total_rows)
        add_unique_ska_distance(distance, seen, pending)
        last_row = row_number
        if len(pending) >= batch_size:
            with transaction(connection):
                _insert_ska_distance_batch(connection, pending)
            imported += len(pending)
            pending.clear()
            save_ska_checkpoint(config, master_id, csv_path, last_row)
    if pending:
        with transaction(connection):
            _insert_ska_distance_batch(connection, pending)
        imported += len(pending)
        save_ska_checkpoint(config, master_id, csv_path, last_row)
    checkpoint = ska_checkpoint_path(config, master_id)
    checkpoint.unlink(missing_ok=True)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM ska_distance WHERE ska_master_id = %s",
            (str(master_id),),
        )
        total_imported = cursor.fetchone()[0]
    _progress(progress, "SKA: importing distances", total_rows, total_rows)
    return {
        "ska_master_id": master_id,
        "task_id": task["task_id"],
        "sample_count": len(mapped_samples),
        "distance_count": total_imported,
        "rows_processed_this_run": max(0, total_rows - start_row),
        "rows_resumed_from": start_row,
        "distance_rows_written_this_run": imported,
        "missing_samples": [],
    }


def verify_mash_ska(
    connection,
    config: ImportConfig,
    gcs_client=None,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Verify imported MASH/SKA rows, membership, mappings, and GCS object."""

    context = lookup_global_context(connection)
    if context is None:
        return {
            "ok": False,
            "global_batch_exists": False,
            "global_run_exists": False,
            "error": (
                f"{GLOBAL_BATCH_ACCESSION} and {GLOBAL_RUN_ACCESSION} do not exist."
            ),
        }
    batch_id, _ = context
    mash_id = ids.mash_master_id(batch_id, config.mash_ska.mash_version)
    ska_id = ids.ska_master_id(batch_id, config.mash_ska.ska_version)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT mm.artifact_id, mm.batch_sketch_count, a.uri
            FROM mash_master mm
            LEFT JOIN artifacts a ON a.artifact_id = mm.artifact_id
            WHERE mm.mash_master_id = %s
            """,
            (str(mash_id),),
        )
        mash_row = cursor.fetchone()
        cursor.execute(
            "SELECT count(*) FROM mash_master_samples WHERE mash_master_id = %s",
            (str(mash_id),),
        )
        membership_count = cursor.fetchone()[0]
        cursor.execute(
            "SELECT sample_count FROM ska_master WHERE ska_master_id = %s",
            (str(ska_id),),
        )
        ska_row = cursor.fetchone()
        cursor.execute(
            "SELECT count(*) FROM ska_distance WHERE ska_master_id = %s",
            (str(ska_id),),
        )
        distance_count = cursor.fetchone()[0]
    missing_report: dict[str, Any] = {"ok": True, "missing_samples": []}
    expected_members = None
    expected_distances = None
    try:
        _progress(progress, "Verify: counting SKA rows", 0, 1)
        total_rows = count_ska_csv_rows(config)
        _progress(progress, "Verify: counting SKA rows", 1, 1)
        records = load_sample_records(
            connection,
            config.mash_ska.sample_source.source_filter,
        )
        index, ambiguous = build_sample_index(records)
        apply_ska_sample_overrides(config, records, index, ambiguous)
        map_sample_names(
            ska_csv_sample_names(config, progress, total_rows),
            index,
            ambiguous,
        )
        expected_members = len(mash_membership_records(connection, config))
        columns = config.mash_ska.ska_csv_columns
        seen: set[bytes] = set()
        _, rows = _ska_reader(config)
        for row_number, raw in enumerate(rows, 1):
            first = index[raw[columns.sample1].strip()]
            second = index[raw[columns.sample2].strip()]
            identifier = ids.ska_distance_id(
                ska_id,
                first.sample_id,
                second.sample_id,
            )
            seen.add(identifier.bytes)
            if row_number % 10000 == 0 or row_number == total_rows:
                _progress(
                    progress,
                    "Verify: checking SKA pairs",
                    row_number,
                    total_rows,
                )
        expected_distances = len(seen)
    except MissingSamplesError as exc:
        missing_report = exc.report()
    uri = mash_row[2] if mash_row else None
    gcs_exists = False
    if uri:
        client = gcs_client or client_from_config(config)
        gcs_exists = object_exists(client, uri)
    report = {
        "global_batch_exists": True,
        "global_run_exists": True,
        "mash_master_exists": mash_row is not None,
        "mash_artifact_exists": bool(mash_row and mash_row[0]),
        "mash_gcs_object_exists": gcs_exists,
        "mash_master_samples_count": membership_count,
        "mash_expected_sample_count": expected_members,
        "mash_membership_matches": (
            expected_members is not None and membership_count == expected_members
        ),
        "ska_master_exists": ska_row is not None,
        "ska_expected_sample_count": ska_row[0] if ska_row else None,
        "ska_distance_count": distance_count,
        "ska_expected_distance_count": expected_distances,
        "ska_distance_count_matches": (
            expected_distances is not None and distance_count == expected_distances
        ),
        "missing_sample_mappings": missing_report,
    }
    report["ok"] = all(
        (
            report["mash_master_exists"],
            report["mash_artifact_exists"],
            report["mash_gcs_object_exists"],
            report["mash_membership_matches"],
            report["ska_master_exists"],
            report["ska_distance_count_matches"],
            missing_report["ok"],
        )
    )
    return report
