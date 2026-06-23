"""Build and upload a per-sample summary from normalized import results."""

from __future__ import annotations

import csv
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID

from .config import ImportConfig
from .import_artifacts import import_artifact

SUMMARY_FIELDS = [
    "sample_id",
    "sample_name",
    "batch_id",
    "upload_date",
    "genus",
    "top_species",
    "top_species_percent",
    "tax_tree",
    "mlst_species",
    "mlst_st",
    "allelic_profile",
    "genome_length",
    "gc_content",
    "contigs_total",
    "n50",
    "l50",
    "avg_coverage",
    "percent_mapped",
    "gene_count",
    "amr_gene_count",
    "top_amr_gene",
    "qc1_decision",
    "qc2_decision",
    "overall_qc",
]


def _first(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return rows[0] if rows else {}


def _top_bracken(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return max(
        rows,
        key=lambda row: (
            row.get("genus_percent_sum") is not None,
            row.get("genus_percent_sum") or 0,
        ),
        default={},
    )


def _allelic_profile(value: Any) -> Any:
    if isinstance(value, list):
        return ";".join(str(item) for item in value)
    return value


def build_summary_row(
    sample: dict[str, Any],
    results: dict[str, list[dict[str, Any]]],
    qc1_row: dict[str, Any] | None,
    qc2_row: dict[str, Any] | None,
) -> dict[str, Any]:
    """Aggregate the normalized extractor and QC rows into one CSV record."""

    bracken = _top_bracken(results.get("bracken", []))
    mlst = _first(results.get("mlst", []))
    quast = _first(results.get("quast", []))
    bbmap = _first(results.get("bbmap", []))
    annotations = results.get("prokka", [])
    amr = results.get("amrfinder", [])
    taxonomy = [
        bracken.get(name)
        for name in (
            "domain",
            "phylum",
            "class",
            "order",
            "family",
            "genus",
            "top_species_name",
        )
        if bracken.get(name)
    ]
    qc1_decision = qc1_row.get("qc1_decision") if qc1_row else None
    qc2_decision = qc2_row.get("qc2_decision") if qc2_row else None
    if "FAIL" in {qc1_decision, qc2_decision}:
        overall_qc = "FAIL"
    elif "WARN" in {qc1_decision, qc2_decision}:
        overall_qc = "WARN"
    else:
        overall_qc = "PASS"
    genes = [str(row["gene"]) for row in amr if row.get("gene")]

    return {
        "sample_id": sample["sample_id"],
        "sample_name": sample["sample_name"],
        "batch_id": sample["batch_id"],
        "upload_date": sample.get("created_at"),
        "genus": bracken.get("genus"),
        "top_species": bracken.get("top_species_name"),
        "top_species_percent": bracken.get("top_species_percent"),
        "tax_tree": "/".join(str(value) for value in taxonomy),
        "mlst_species": mlst.get("mlst_species") or mlst.get("species"),
        "mlst_st": mlst.get("st_type"),
        "allelic_profile": _allelic_profile(mlst.get("allelic_profile")),
        "genome_length": quast.get("total_length"),
        "gc_content": quast.get("gc_percent"),
        "contigs_total": quast.get("contigs_total"),
        "n50": quast.get("n50"),
        "l50": quast.get("l50"),
        "avg_coverage": bbmap.get("avg_coverage"),
        "percent_mapped": bbmap.get("percent_mapped"),
        "gene_count": len(annotations),
        "amr_gene_count": len(amr),
        "top_amr_gene": max(genes) if genes else None,
        "qc1_decision": qc1_decision,
        "qc2_decision": qc2_decision,
        "overall_qc": overall_qc,
    }


def _write_summary_csv(
    summary: dict[str, Any],
    sample_name: str,
    directory: Path,
) -> Path:
    """Write one normalized summary record with a stable column order."""

    output = directory / f"{sample_name}_summary.csv"
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        writer.writerow(summary)
    return output


def export_summary_artifact(
    connection,
    gcs_client,
    config: ImportConfig,
    run_id: UUID,
    sample_id: UUID,
    sample_name: str,
    summary: dict[str, Any],
    upload: bool = True,
):
    """Create the export_summary task and upload its generated CSV archive."""

    with tempfile.TemporaryDirectory(prefix="legacy-summary-") as temp_dir:
        root = Path(temp_dir)
        summary_path = _write_summary_csv(summary, sample_name, root)
        return import_artifact(
            connection,
            gcs_client,
            config,
            root,
            run_id,
            sample_id,
            "export_summary",
            [summary_path],
            upload=upload,
        )
