"""Typer command-line interface for inventory, import, and verification."""

from __future__ import annotations

import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter, sleep
from typing import Any

import typer

from . import ids
from .config import ImportConfig, load_config
from .db import (
    connect,
    is_transient_connection_error,
    safe_close,
    safe_rollback,
    test_database,
)
from .discovery import discover_gcp_inputs, discover_sample
from .gcs import test_gcp
from .import_ownership import ownership_rows
from .import_reads import raw_read_object_name
from .import_mash_ska import (
    MissingSamplesError,
    import_mash_master,
    import_ska_distances,
    verify_mash_ska,
)
from .import_sample import import_sample
from .logging_config import configure_logging, safe_error
from .manifest import ManifestRow, read_manifest
from .schema import inspect_schema
from .timing import PhaseTimer
from .verify import verify_sample

app = typer.Typer(no_args_is_help=True, help="Import historical sequencing outputs safely.")
LOG = logging.getLogger(__name__)


class _TerminalProgress:
    """Render one compact progress bar for long MASH/SKA operations."""

    def __init__(self, enabled: bool = True, width: int = 30):
        self.enabled = enabled
        self.width = width
        self.label: str | None = None
        self.finished = True

    def __call__(self, label: str, current: int, total: int) -> None:
        if not self.enabled:
            return
        if self.label != label:
            if not self.finished:
                typer.echo()
            self.label = label
        total = max(total, 1)
        current = min(max(current, 0), total)
        ratio = current / total
        filled = int(self.width * ratio)
        bar = "#" * filled + "-" * (self.width - filled)
        typer.echo(
            f"\r{label:<32} [{bar}] {ratio:6.1%} "
            f"({current:,}/{total:,})",
            nl=False,
        )
        self.finished = current >= total
        if self.finished:
            typer.echo()

    def close(self) -> None:
        """Finish the current terminal line after an early stop."""

        if self.enabled and not self.finished:
            typer.echo()
            self.finished = True


def _print(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, default=str))


def _rows(csv_path: Path, organization: str | None, sample_name: str | None, limit: int | None):
    return read_manifest(csv_path, organization=organization, sample_name=sample_name, limit=limit)


def _protect_source_paths(output: Path, rows: list[ManifestRow]) -> None:
    """Reject generated output paths inside any manifest source directory."""

    resolved_output = output.resolve()
    for row in rows:
        source = row.full_path.resolve()
        if resolved_output == source or resolved_output.is_relative_to(source):
            raise ValueError(
                f"Refusing to write generated output inside source data: "
                f"{resolved_output}"
            )


def _append_failure_report(path: Path, failure: dict[str, Any]) -> None:
    """Append one durable JSON failure record for later review."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(failure, default=str) + "\n")


def _emit_timing_breakdown(
    sample_name: str,
    timings: dict[str, float],
    total_seconds: float,
) -> dict[str, float]:
    """Print and log a readable non-overlapping phase timing breakdown."""

    normalized = dict(timings)
    measured = sum(normalized.values())
    overhead = max(0.0, total_seconds - measured)
    if overhead >= 0.001:
        normalized["untracked_overhead"] = round(overhead, 3)
    labels = {
        "input_discovery": "input_discovery",
        "archive_creation": "archive_creation",
        "checksum": "checksum",
        "gcs_upload": "gcs_upload",
        "database_connection": "database_connection",
        "database_query": "database_query",
        "database_write": "database_write",
        "extraction": "extraction",
        "qc_verification": "qc/verification",
        "retry_backoff": "retry_backoff",
        "other": "other",
        "untracked_overhead": "untracked_overhead",
    }
    typer.echo("  timings:")
    for phase, seconds in normalized.items():
        typer.echo(f"    {labels.get(phase, phase)}: {seconds:.1f}s")
    LOG.info("Sample %s phase timings: %s", sample_name, normalized)
    return normalized


@app.command("test-db")
def test_db(
    config: Path = typer.Option(..., exists=True),
    write_test: bool = typer.Option(False, help="Run a rolled-back write test."),
) -> None:
    """Test PostgreSQL access and validate required tables and columns."""

    configure_logging()
    _print(test_database(load_config(config), write_test))


@app.command("test-gcp")
def test_gcp_command(config: Path = typer.Option(..., exists=True)) -> None:
    """Test GCS authentication and object lifecycle access."""

    configure_logging()
    _print(test_gcp(load_config(config)))


@app.command("test-connections")
def test_connections(config: Path = typer.Option(..., exists=True)) -> None:
    """Test PostgreSQL and GCS connections."""

    configure_logging()
    settings = load_config(config)
    _print({"database": test_database(settings), "gcp": test_gcp(settings)})


@app.command("inspect-schema")
def inspect_schema_command(config: Path = typer.Option(..., exists=True)) -> None:
    """Report missing required database tables and columns."""

    settings = load_config(config)
    connection = connect(settings)
    try:
        report = inspect_schema(connection)
        _print(report)
        if not report["ok"]:
            raise typer.Exit(1)
    finally:
        connection.close()


def _inventory_records(rows: list[ManifestRow], settings: ImportConfig) -> list[dict[str, Any]]:
    """Build flat inventory records suitable for CSV and JSON."""

    records = []
    for row in rows:
        inventory = discover_sample(row.full_path, settings)
        data = inventory.as_dict()
        records.append({
            **row.source_metadata(),
            "path_exists": data["exists"],
            "r1": data["r1"],
            "r2": data["r2"],
            "available_tools": ",".join(name for name, paths in inventory.tools.items() if paths),
            "tool_files": json.dumps(data["tools"]),
            "supplemental_files": json.dumps(data["supplemental"]),
            "missing": ",".join(inventory.missing),
        })
    return records


@app.command()
def inventory(
    csv_path: Path = typer.Option(..., "--csv", exists=True),
    config: Path = typer.Option(..., exists=True),
    out: Path | None = typer.Option(None),
    organization: str | None = None,
    sample_name: str | None = None,
    limit: int | None = None,
) -> None:
    """Scan local files only and write a CSV or JSON inventory report."""

    rows = _rows(csv_path, organization, sample_name, limit)
    if out is not None:
        _protect_source_paths(out, rows)
    records = _inventory_records(rows, load_config(config))
    if out is None:
        _print(records)
    elif out.suffix.lower() == ".json":
        out.write_text(json.dumps(records, indent=2, default=str), encoding="utf-8")
    else:
        with out.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(records[0]) if records else [])
            if records:
                writer.writeheader()
                writer.writerows(records)
    typer.echo(f"Inventoried {len(records)} sample(s).")


@app.command("dry-run")
def dry_run(
    csv_path: Path = typer.Option(..., "--csv", exists=True),
    config: Path = typer.Option(..., exists=True),
    limit: int | None = None,
    organization: str | None = None,
    sample_name: str | None = None,
) -> None:
    """Validate schema and show planned IDs, uploads, extraction, and QC work."""

    settings = load_config(config)
    connection = connect(settings)
    try:
        schema_report = inspect_schema(connection)
    finally:
        connection.close()
    plans = []
    for row in _rows(csv_path, organization, sample_name, limit):
        inventory = discover_sample(row.full_path, settings)
        ownership = ownership_rows(row, settings)
        sample = ownership["samples"]["sample_id"]
        run = ownership["runs"]["run_id"]
        plans.append({
            "sample_name": row.sample_name,
            "ids": {name: str(values[next(key for key in values if key.endswith("_id"))])
                    for name, values in ownership.items()},
            "reads": [
                {
                    "read_id": str(ids.read_id(sample, read_type, path.name)),
                    "source": str(path),
                    "destination": (
                        f"gs://{settings.gcp.raw_reads_bucket}/"
                        f"{raw_read_object_name(settings, row, path.name)}"
                    ),
                }
                for read_type, path in inventory.reads.items()
            ],
            "tools": [
                {
                    "tool": tool,
                    "task_id": str(ids.task_id(sample, tool)),
                    "artifact_id": str(ids.artifact_id(sample, tool, "output")),
                    "destination": (
                        f"gs://{settings.gcp.artifact_bucket}/{run}/{sample}/"
                        f"{ids.task_id(sample, tool)}.tar.gz"
                        if (
                            discover_gcp_inputs(inventory.sample_dir, settings, tool)
                            or tool == "export_summary"
                        )
                        else None
                    ),
                    "files": [
                        str(path)
                        for path in discover_gcp_inputs(
                            inventory.sample_dir,
                            settings,
                            tool,
                        )
                    ],
                    "extractor": (
                        tool
                        if tool_config.extractor_enabled
                        and inventory.tools.get(tool)
                        else None
                    ),
                }
                for tool, tool_config in settings.tools.items()
                if tool_config.enabled
                and (
                    inventory.tools.get(tool)
                    or discover_gcp_inputs(inventory.sample_dir, settings, tool)
                    or tool == "export_summary"
                )
            ],
            "qc": ["qc1", "qc2"],
        })
    _print({"schema": schema_report, "committed": False, "uploaded": False, "samples": plans})


def _sample_exists(connection, sample_id) -> bool:
    """Check whether a deterministic sample ID is already present."""

    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM samples WHERE sample_id = %s", (str(sample_id),))
        return cursor.fetchone() is not None


def _load_existing_sample_ids(
    settings: ImportConfig,
    sample_ids: list,
    *,
    database_retries: int,
    retry_base_seconds: float,
    chunk_size: int = 5000,
) -> set[str]:
    """Load selected existing sample IDs once to avoid per-skip connections."""

    if not sample_ids:
        return set()
    attempts = database_retries + 1
    for attempt in range(1, attempts + 1):
        connection = None
        try:
            connection = connect(settings)
            existing: set[str] = set()
            with connection.cursor() as cursor:
                for start in range(0, len(sample_ids), chunk_size):
                    chunk = [str(value) for value in sample_ids[start:start + chunk_size]]
                    cursor.execute(
                        "SELECT sample_id FROM samples WHERE sample_id = ANY(%s::uuid[])",
                        (chunk,),
                    )
                    existing.update(str(row[0]) for row in cursor.fetchall())
            return existing
        except Exception as exc:
            safe_rollback(connection)
            if not is_transient_connection_error(exc) or attempt >= attempts:
                raise
            delay = retry_base_seconds * (2 ** (attempt - 1))
            LOG.warning(
                "Database connection lost while loading existing samples; "
                "retrying attempt %d/%d in %.1fs.",
                attempt + 1,
                attempts,
                delay,
            )
            sleep(delay)
        finally:
            safe_close(connection)
    raise RuntimeError("unreachable")


def _import_one_sample(
    settings: ImportConfig,
    row: ManifestRow,
    sample_identifier,
    *,
    skip_existing: bool,
    skip_reads: bool,
    skip_artifacts: bool,
    skip_extractors: bool,
    skip_qc: bool,
    database_retries: int,
    retry_base_seconds: float,
) -> tuple[str, dict[str, Any], int]:
    """Import one sample using a fresh connection and retry dropped sessions."""

    attempts = database_retries + 1
    timings = PhaseTimer()
    for attempt in range(1, attempts + 1):
        connection = None
        try:
            with timings.measure("database_connection"):
                connection = connect(settings)
            if skip_existing:
                with timings.measure("database_query"):
                    exists = _sample_exists(connection, sample_identifier)
            else:
                exists = False
            if exists:
                return "skipped_existing", {"sample": row.sample_name}, attempt
            report = import_sample(
                connection,
                settings,
                row,
                skip_reads=skip_reads,
                skip_artifacts=skip_artifacts,
                skip_extractors=skip_extractors,
                skip_qc=skip_qc,
                timings=timings,
            )
            return "imported", report, attempt
        except Exception as exc:
            safe_rollback(connection)
            if not is_transient_connection_error(exc) or attempt >= attempts:
                raise
            delay = retry_base_seconds * (2 ** (attempt - 1))
            LOG.warning(
                "Database connection lost for sample %s; retrying attempt %d/%d "
                "in %.1fs.",
                row.sample_name,
                attempt + 1,
                attempts,
                delay,
            )
            with timings.measure("retry_backoff"):
                sleep(delay)
        finally:
            safe_close(connection)
    raise RuntimeError("unreachable")


@app.command("import")
def import_command(
    csv_path: Path = typer.Option(..., "--csv", exists=True),
    config: Path = typer.Option(..., exists=True),
    limit: int | None = None,
    organization: str | None = None,
    sample_name: str | None = None,
    resume: bool = True,
    skip_existing: bool = False,
    skip_reads: bool = False,
    skip_artifacts: bool = False,
    skip_extractors: bool = False,
    skip_qc: bool = False,
    progress: bool = typer.Option(True, "--progress/--no-progress"),
    database_retries: int = typer.Option(
        3,
        "--database-retries",
        min=0,
        help="Retries per sample after transient PostgreSQL connection failures.",
    ),
    retry_base_seconds: float = typer.Option(
        2.0,
        "--retry-base-seconds",
        min=0.0,
        help="Initial database retry delay; subsequent delays double.",
    ),
    failure_report: Path = typer.Option(
        Path(".legacy-import-state/sample_failures.jsonl"),
        "--failure-report",
        help="Append durable per-sample errors to this JSONL file.",
    ),
) -> None:
    """Import selected samples and emit a readable per-sample failure report."""

    configure_logging()
    settings = load_config(config)
    rows = _rows(csv_path, organization, sample_name, limit)
    _protect_source_paths(failure_report, rows)
    sample_identifiers = [
        ids.sample_id(
            row.organization_code,
            row.legacy_batch_code,
            row.sample_name,
        )
        for row in rows
    ]
    existing_sample_ids = (
        _load_existing_sample_ids(
            settings,
            sample_identifiers,
            database_retries=database_retries,
            retry_base_seconds=retry_base_seconds,
        )
        if (skip_existing or resume)
        else set()
    )
    reports, failures = [], []
    reporter = _TerminalProgress(progress)
    if rows:
        reporter("Samples: uploading", 0, len(rows))
    try:
        for row_number, (row, sample_identifier) in enumerate(
            zip(rows, sample_identifiers),
            1,
        ):
            started = perf_counter()
            if str(sample_identifier) in existing_sample_ids:
                elapsed = perf_counter() - started
                reports.append({
                    "sample": row.sample_name,
                    "status": "skipped_existing",
                    "elapsed_seconds": round(elapsed, 3),
                    "database_attempts": 0,
                })
                reporter("Samples: uploading", row_number, len(rows))
                reporter.close()
                typer.echo(f"{row.sample_name}: skipped existing ({elapsed:.1f}s)")
                continue
            try:
                status, report, attempts = _import_one_sample(
                    settings,
                    row,
                    sample_identifier,
                    skip_existing=False,
                    skip_reads=skip_reads,
                    skip_artifacts=skip_artifacts,
                    skip_extractors=skip_extractors,
                    skip_qc=skip_qc,
                    database_retries=database_retries,
                    retry_base_seconds=retry_base_seconds,
                )
                elapsed = perf_counter() - started
                if status == "imported":
                    existing_sample_ids.add(str(sample_identifier))
                    typer.echo(
                        f"{row.sample_name}: imported in {elapsed:.1f}s "
                        f"(database attempts: {attempts})"
                    )
                    report["timings"] = _emit_timing_breakdown(
                        row.sample_name,
                        report.get("timings", {}),
                        elapsed,
                    )
                reports.append({
                    "status": status,
                    "elapsed_seconds": round(elapsed, 3),
                    "database_attempts": attempts,
                    **report,
                })
                reporter("Samples: uploading", row_number, len(rows))
                reporter.close()
                if status == "skipped_existing":
                    typer.echo(f"{row.sample_name}: skipped existing ({elapsed:.1f}s)")
            except Exception as exc:
                elapsed = perf_counter() - started
                failure = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "sample": row.sample_name,
                    "organization_code": row.organization_code,
                    "legacy_batch_code": row.legacy_batch_code,
                    "elapsed_seconds": round(elapsed, 3),
                    "error": safe_error(exc),
                }
                failures.append(failure)
                _append_failure_report(failure_report, failure)
                LOG.error("Sample %s failed: %s", row.sample_name, failure["error"])
                reporter("Samples: uploading", row_number, len(rows))
                reporter.close()
                typer.echo(f"{row.sample_name}: failed after {elapsed:.1f}s")
                if not resume:
                    break
    finally:
        reporter.close()
    _print({"imported": reports, "failures": failures})
    if failures:
        raise typer.Exit(1)


@app.command()
def verify(
    csv_path: Path = typer.Option(..., "--csv", exists=True),
    config: Path = typer.Option(..., exists=True),
    limit: int | None = None,
    organization: str | None = None,
    sample_name: str | None = None,
) -> None:
    """Verify imported rows, result records, QC, and referenced GCS objects."""

    settings = load_config(config)
    connection = connect(settings)
    reports = []
    try:
        for row in _rows(csv_path, organization, sample_name, limit):
            ownership = ownership_rows(row, settings)
            reports.append({
                "sample": row.sample_name,
                **verify_sample(connection, settings, {
                    "organization_code": row.organization_code,
                    "sample_id": ownership["samples"]["sample_id"],
                }),
            })
    finally:
        connection.close()
    _print(reports)
    if not all(report["ok"] for report in reports):
        raise typer.Exit(1)


@app.command("import-mash-master")
def import_mash_master_command(
    config: Path = typer.Option(..., exists=True),
    progress: bool = typer.Option(True, "--progress/--no-progress"),
) -> None:
    """Register and upload the existing legacy global MASH master."""

    configure_logging()
    settings = load_config(config)
    connection = connect(settings)
    try:
        _print(import_mash_master(
            connection,
            settings,
            progress=_TerminalProgress(progress),
        ))
    except MissingSamplesError as exc:
        _print(exc.report())
        raise typer.Exit(1) from exc
    finally:
        connection.close()


@app.command("import-ska-distances")
def import_ska_distances_command(
    config: Path = typer.Option(..., exists=True),
    progress: bool = typer.Option(True, "--progress/--no-progress"),
    resume: bool = typer.Option(True, "--resume/--no-resume"),
) -> None:
    """Import existing precomputed legacy SKA distances."""

    configure_logging()
    settings = load_config(config)
    connection = connect(settings)
    try:
        _print(import_ska_distances(
            connection,
            settings,
            progress=_TerminalProgress(progress),
            resume=resume,
        ))
    except MissingSamplesError as exc:
        _print(exc.report())
        raise typer.Exit(1) from exc
    finally:
        connection.close()


@app.command("import-mash-ska")
def import_mash_ska_command(
    config: Path = typer.Option(..., exists=True),
    progress: bool = typer.Option(True, "--progress/--no-progress"),
    resume: bool = typer.Option(True, "--resume/--no-resume"),
) -> None:
    """Import the legacy MASH master and precomputed SKA distances."""

    configure_logging()
    settings = load_config(config)
    connection = connect(settings)
    try:
        reporter = _TerminalProgress(progress)
        mash_report = import_mash_master(connection, settings, progress=reporter)
        ska_report = import_ska_distances(
            connection,
            settings,
            progress=reporter,
            resume=resume,
        )
        _print({"mash": mash_report, "ska": ska_report})
    except MissingSamplesError as exc:
        _print(exc.report())
        raise typer.Exit(1) from exc
    finally:
        connection.close()


@app.command("verify-mash-ska")
def verify_mash_ska_command(
    config: Path = typer.Option(..., exists=True),
    progress: bool = typer.Option(True, "--progress/--no-progress"),
) -> None:
    """Verify legacy MASH/SKA rows, membership, mappings, and GCS."""

    settings = load_config(config)
    connection = connect(settings)
    try:
        report = verify_mash_ska(
            connection,
            settings,
            progress=_TerminalProgress(progress),
        )
    finally:
        connection.close()
    _print(report)
    if not report["ok"]:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
