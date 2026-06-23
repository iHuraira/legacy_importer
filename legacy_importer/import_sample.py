"""Orchestrate one sample as an explicit transactional import unit."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .config import ImportConfig
from .db import fetch_rows, transaction, upsert
from .discovery import discover_gcp_inputs, discover_sample
from .gcs import client_from_config
from .import_artifacts import import_artifact, task_row
from .export_summary import build_summary_row, export_summary_artifact
from .import_ownership import import_ownership, ownership_rows
from .import_reads import import_reads
from .import_results import ExtractorError, import_tool_results
from .manifest import ManifestRow
from .qc import build_qc1_row, build_qc2_row


def import_sample(
    connection,
    config: ImportConfig,
    row: ManifestRow,
    *,
    skip_reads: bool = False,
    skip_artifacts: bool = False,
    skip_extractors: bool = False,
    skip_qc: bool = False,
) -> dict[str, Any]:
    """Import one sample atomically; uploaded objects remain safe to reuse on retry."""

    inventory = discover_sample(row.full_path, config)
    if not inventory.sample_dir.is_dir():
        raise FileNotFoundError(row.full_path)
    ownership = ownership_rows(row, config)
    sample_id = ownership["samples"]["sample_id"]
    run_id = ownership["runs"]["run_id"]
    report: dict[str, Any] = {"sample": row.sample_name, "reads": [], "tools": {}, "warnings": []}
    gcs_client = None
    needs_gcs = (
        (not skip_reads and config.reads.get("upload", True))
        or (not skip_artifacts and config.artifacts.get("upload", True))
    )
    if needs_gcs:
        gcs_client = client_from_config(config)

    with transaction(connection):
        import_ownership(connection, ownership)
        reads = {}
        if not skip_reads:
            reads = import_reads(
                connection, gcs_client, config, row, sample_id, inventory.reads,
                upload=config.reads.get("upload", True),
            )
            report["reads"] = sorted(reads)
        else:
            reads = {
                record["read_type"]: record
                for record in fetch_rows(connection, "reads", sample_id)
                if record.get("read_type") in {"R1", "R2"}
            }

        results: dict[str, list[dict[str, Any]]] = {}
        for tool_name, tool_config in config.tools.items():
            if tool_name == "export_summary" or not tool_config.enabled:
                continue
            paths = inventory.tools.get(tool_name, [])
            gcp_paths = discover_gcp_inputs(
                inventory.sample_dir,
                config,
                tool_name,
            )
            if not paths and not gcp_paths:
                continue
            if skip_artifacts or not gcp_paths:
                task = task_row(
                    config, run_id, sample_id, tool_name, datetime.now(timezone.utc)
                )
                upsert(connection, "tasks", task, ["task_id"])
                artifact = None
                archive = None
            else:
                task, artifact, archive = import_artifact(
                    connection, gcs_client, config, inventory.sample_dir, run_id, sample_id,
                    tool_name, gcp_paths, upload=config.artifacts.get("upload", True),
                )
            tool_report = {
                "artifact_uri": artifact["uri"] if artifact else None,
                "results": 0,
            }
            if not skip_extractors and tool_config.extractor_enabled and paths:
                try:
                    parsed = import_tool_results(
                        connection, tool_name, paths, sample_id, task["task_id"], reads,
                        config.tools[tool_name].result_table,
                    )
                    results[tool_name] = parsed
                    tool_report["results"] = len(parsed)
                except ExtractorError as exc:
                    report["warnings"].append(str(exc))
            report["tools"][tool_name] = tool_report
            if archive:
                archive.unlink(missing_ok=True)

        qc1_row = None
        qc2_row = None
        if not skip_qc:
            # QC can be recalculated from existing result rows when extraction is skipped.
            if skip_extractors:
                for tool_name, tool in config.tools.items():
                    if not tool.extractor_enabled:
                        continue
                    table = tool.result_table or (
                        "bakta_annotations" if tool_name == "prokka" else tool_name
                    )
                    try:
                        existing = fetch_rows(connection, table, sample_id)
                    except Exception:
                        connection.rollback()
                        raise
                    if existing:
                        results[tool_name] = existing
            qc1_gate = config.qc.qc1
            if qc1_gate.enabled:
                qc1_row = build_qc1_row(
                    sample_id,
                    qc1_gate.gate_version,
                    results.get("fastqc", []),
                    reads,
                )
                upsert(connection, "qc1", qc1_row, ["qc1_id"])
                report["qc1"] = qc1_row["qc1_decision"]

            qc2_gate = config.qc.qc2
            if qc2_gate.enabled:
                qc2_row = build_qc2_row(
                    sample_id,
                    qc2_gate.gate_version,
                    results.get("bbmap", []),
                    results.get("quast", []),
                )
                upsert(connection, "qc2", qc2_row, ["qc2_id"])
                report["qc2"] = qc2_row["qc2_decision"]

        summary_config = config.tools.get("export_summary")
        if summary_config and summary_config.enabled:
            if skip_artifacts:
                summary_task = task_row(
                    config,
                    run_id,
                    sample_id,
                    "export_summary",
                    datetime.now(timezone.utc),
                )
                upsert(connection, "tasks", summary_task, ["task_id"])
                report["tools"]["export_summary"] = {
                    "artifact_uri": None,
                    "results": 0,
                }
            else:
                summary_task, summary_artifact, summary_archive = export_summary_artifact(
                    connection,
                    gcs_client,
                    config,
                    run_id,
                    sample_id,
                    row.sample_name,
                    build_summary_row(
                        ownership["samples"],
                        results,
                        qc1_row,
                        qc2_row,
                    ),
                    upload=config.artifacts.get("upload", True),
                )
                report["tools"]["export_summary"] = {
                    "artifact_uri": summary_artifact["uri"],
                    "results": 1,
                }
                summary_archive.unlink(missing_ok=True)
    return report
