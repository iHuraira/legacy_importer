"""Paired-read QC1 calculation and configurable QC2 threshold evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from .ids import qc_id

ALLOWED_READ_LENGTHS = {125, 150, 250, 300}

QC1_REASON_MESSAGES = {
    "read_length": {
        "FAIL": "Read length is missing or outside the supported range.",
    },
    "coverage_small": {
        "WARN": "Coverage is lower than recommended for small genome analysis.",
        "FAIL": "Coverage is too low for reliable small genome analysis.",
    },
    "coverage_big": {
        "WARN": "Coverage is lower than recommended for large genome analysis.",
        "FAIL": "Coverage is too low for reliable large genome analysis.",
    },
    "duplication_rate": {
        "WARN": "Duplicate read levels are higher than expected.",
        "FAIL": "Duplicate read levels are too high for reliable analysis.",
    },
    "n_content": {
        "WARN": "The sample contains an elevated number of ambiguous bases (N).",
        "FAIL": "The sample contains too many ambiguous bases (N).",
    },
    "gc_content": {
        "FAIL": "GC content is outside the expected range for this sample.",
    },
    "read_imbalance_ratio": {
        "WARN": "R1 and R2 read counts are slightly imbalanced.",
        "FAIL": "R1 and R2 read counts are severely imbalanced.",
    },
}

QC2_REASON_MESSAGES = {
    "mean_coverage": {
        "WARN": "Mean coverage is lower than recommended.",
        "FAIL": "Mean coverage is too low for reliable analysis.",
    },
    "percent_mapped": {
        "WARN": "A slightly lower than expected percentage of reads mapped to the reference.",
        "FAIL": "Too few reads mapped to the reference for reliable analysis.",
    },
    "breadth_10x": {
        "WARN": "Reference coverage breadth is slightly lower than recommended.",
        "FAIL": "Reference coverage breadth is too low for reliable analysis.",
    },
    "total_length": {
        "WARN": "Assembly size is outside the expected range.",
        "FAIL": "Assembly size is far outside the expected range.",
    },
    "contigs_total": {
        "WARN": "The assembly has more contigs than expected.",
        "FAIL": "The assembly is highly fragmented.",
    },
    "largest_contig": {
        "WARN": "The largest contig is smaller than recommended.",
        "FAIL": "The largest contig is too small for a reliable assembly.",
    },
    "n50": {
        "WARN": "Assembly N50 is lower than recommended.",
        "FAIL": "Assembly N50 is too low for reliable analysis.",
    },
    "gc_percent": {
        "FAIL": "Assembly GC content is outside the expected range.",
    },
}


@dataclass(frozen=True)
class QCResult:
    decision: str
    metric_values: dict[str, Any]
    metric_statuses: dict[str, str]
    reasons_fail: list[str]
    reasons_warn: list[str]


def evaluate_thresholds(
    metrics: dict[str, Any], thresholds: dict[str, float | int]
) -> QCResult:
    """Evaluate min_/max_ thresholds, warning when a metric is unavailable."""

    statuses: dict[str, str] = {}
    failures: list[str] = []
    warnings: list[str] = []
    for rule, threshold in thresholds.items():
        if rule.startswith("min_"):
            metric, comparator = rule[4:], lambda value: value >= threshold
        elif rule.startswith("max_"):
            metric, comparator = rule[4:], lambda value: value <= threshold
        else:
            warnings.append(f"Unsupported threshold rule: {rule}")
            continue
        value = metrics.get(metric)
        if value is None:
            statuses[metric] = "WARN"
            warnings.append(f"{metric} is unavailable")
        elif comparator(value):
            statuses[metric] = "PASS"
        else:
            statuses[metric] = "FAIL"
            failures.append(f"{metric}={value} violates {rule}={threshold}")
    decision = "FAIL" if failures else ("WARN" if warnings else "PASS")
    return QCResult(decision, metrics, statuses, failures, warnings)


def build_qc_row(
    gate_name: str,
    sample_id: UUID,
    gate_version: str,
    metrics: dict[str, Any],
    thresholds: dict[str, float | int],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build an idempotent database row for either QC gate."""

    result = evaluate_thresholds(metrics, thresholds)
    prefix = gate_name.lower()
    return {
        f"{prefix}_id": qc_id(prefix, sample_id, gate_version),
        "sample_id": sample_id,
        f"{prefix}_decision": result.decision,
        f"{prefix}_gate_version": gate_version,
        "metric_values": result.metric_values,
        "metric_statuses": result.metric_statuses,
        "reasons_fail": result.reasons_fail,
        "reasons_warn": result.reasons_warn,
        "qc_timestamp": now or datetime.now(timezone.utc),
    }


def _fraction(value: Any) -> float:
    """Accept extractor fractions and legacy percentage values."""

    number = float(value or 0)
    return number / 100 if number > 1 else number


def _read_type(
    row: dict[str, Any],
    reads: dict[str, dict[str, Any]] | None,
) -> str | None:
    explicit = str(row.get("read_type", "")).upper()
    if explicit in {"R1", "R2"}:
        return explicit
    if reads and row.get("read_id") is not None:
        row_read_id = str(row["read_id"])
        for read_type in ("R1", "R2"):
            if row_read_id == str(reads.get(read_type, {}).get("read_id")):
                return read_type
    return None


def calculate_qc1(
    fastqc_rows: list[dict[str, Any]],
    reads: dict[str, dict[str, Any]] | None = None,
) -> QCResult:
    """Calculate QC1 from paired R1/R2 FastQC rows."""

    typed_rows = {
        read_type: row
        for row in fastqc_rows
        if (read_type := _read_type(row, reads)) is not None
    }
    if "R1" not in typed_rows or "R2" not in typed_rows:
        raise RuntimeError("QC1 requires both R1 and R2 FastQC rows")

    r1, r2 = typed_rows["R1"], typed_rows["R2"]
    try:
        total_reads_r1 = int(r1["total_reads"])
        total_reads_r2 = int(r2["total_reads"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("QC1 requires total_reads for both R1 and R2") from exc

    statuses: dict[str, str] = {}
    failures: list[str] = []
    warnings: list[str] = []
    read_length_r1 = r1.get("read_length")
    read_length_r2 = r2.get("read_length")
    if (
        not read_length_r1
        or not read_length_r2
        or read_length_r1 not in ALLOWED_READ_LENGTHS
        or read_length_r2 not in ALLOWED_READ_LENGTHS
        or read_length_r1 != read_length_r2
    ):
        statuses["read_length"] = "FAIL"
        failures.append("read_length")
        read_length = read_length_r1 or read_length_r2 or 150
    else:
        statuses["read_length"] = "PASS"
        read_length = read_length_r1

    duplication_rate = max(
        _fraction(r1.get("duplication_rate")),
        _fraction(r2.get("duplication_rate")),
    )
    n_content = max(
        _fraction(r1.get("n_content")),
        _fraction(r2.get("n_content")),
    )
    gc_content = (
        _fraction(r1.get("gc_content")) + _fraction(r2.get("gc_content"))
    ) / 2
    paired_reads = min(total_reads_r1, total_reads_r2)
    max_reads = max(total_reads_r1, total_reads_r2)
    read_imbalance_ratio = paired_reads / max_reads if max_reads else 0
    coverage_small = (paired_reads * read_length * 2) / 3_000_000
    coverage_big = (paired_reads * read_length * 2) / 6_000_000

    metric_values = {
        "coverage_small": coverage_small,
        "coverage_big": coverage_big,
        "duplication_rate": duplication_rate,
        "n_content": n_content,
        "gc_content": gc_content,
        "read_imbalance_ratio": read_imbalance_ratio,
        "read_length_used": read_length,
    }

    def evaluate(
        metric: str,
        value: float,
        warn_condition,
        fail_condition,
    ) -> None:
        if fail_condition(value):
            statuses[metric] = "FAIL"
            failures.append(metric)
        elif warn_condition(value):
            statuses[metric] = "WARN"
            warnings.append(metric)
        else:
            statuses[metric] = "PASS"

    evaluate("coverage_small", coverage_small, lambda x: 20 <= x < 30, lambda x: x < 20)
    evaluate("coverage_big", coverage_big, lambda x: 10 <= x < 20, lambda x: x < 10)
    evaluate(
        "duplication_rate",
        duplication_rate,
        lambda x: 0.30 <= x < 0.50,
        lambda x: x >= 0.50,
    )
    evaluate("n_content", n_content, lambda x: 0.01 <= x < 0.05, lambda x: x >= 0.05)

    if gc_content < 0.25 or gc_content > 0.75:
        statuses["gc_content"] = "FAIL"
        failures.append("gc_content")
    else:
        statuses["gc_content"] = "PASS"

    evaluate(
        "read_imbalance_ratio",
        read_imbalance_ratio,
        lambda x: 0.80 <= x < 0.90,
        lambda x: x < 0.80,
    )

    decision = "FAIL" if failures else ("WARN" if warnings else "PASS")
    return QCResult(
        decision=decision,
        metric_values=metric_values,
        metric_statuses=statuses,
        reasons_fail=[
            QC1_REASON_MESSAGES.get(reason, {}).get("FAIL", reason)
            for reason in failures
        ],
        reasons_warn=[
            QC1_REASON_MESSAGES.get(reason, {}).get("WARN", reason)
            for reason in warnings
        ],
    )


def build_qc1_row(
    sample_id: UUID,
    gate_version: str,
    fastqc_rows: list[dict[str, Any]],
    reads: dict[str, dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the persisted QC1 row from paired FastQC results."""

    result = calculate_qc1(fastqc_rows, reads)
    return {
        "qc1_id": qc_id("qc1", sample_id, gate_version),
        "sample_id": sample_id,
        "qc1_decision": result.decision,
        "qc1_gate_version": gate_version,
        "metric_values": result.metric_values,
        "metric_statuses": result.metric_statuses,
        "reasons_fail": result.reasons_fail,
        "reasons_warn": result.reasons_warn,
        "qc_timestamp": now or datetime.now(timezone.utc),
    }


def _required_number(row: dict[str, Any], field: str, source: str) -> float:
    value = row.get(field)
    if value is None:
        raise RuntimeError(f"QC2 requires {source}.{field}")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"QC2 requires numeric {source}.{field}") from exc


def calculate_qc2(
    bbmap_rows: list[dict[str, Any]],
    quast_rows: list[dict[str, Any]],
) -> QCResult:
    """Calculate QC2 from the latest available BBMap and QUAST results."""

    if not bbmap_rows:
        raise RuntimeError("QC2 requires BBMap results")
    if not quast_rows:
        raise RuntimeError("QC2 requires QUAST results")

    bbmap = bbmap_rows[-1]
    quast = quast_rows[-1]
    mean_coverage = _required_number(bbmap, "avg_coverage", "BBMap")
    percent_mapped = _required_number(bbmap, "percent_mapped", "BBMap")
    breadth_10x = _required_number(bbmap, "percent_ref_bases_covered", "BBMap")
    total_length = _required_number(quast, "total_length", "QUAST")
    contigs_total = _required_number(quast, "contigs_total", "QUAST")
    largest_contig = _required_number(quast, "largest_contig", "QUAST")
    n50 = _required_number(quast, "n50", "QUAST")
    gc_percent = _required_number(quast, "gc_percent", "QUAST")
    if gc_percent <= 1:
        gc_percent *= 100

    metric_values = {
        "mean_coverage": mean_coverage,
        "percent_mapped": percent_mapped,
        "breadth_10x": breadth_10x,
        "total_length": total_length,
        "contigs_total": contigs_total,
        "largest_contig": largest_contig,
        "n50": n50,
        "gc_percent": gc_percent,
    }
    statuses: dict[str, str] = {}
    failures: list[str] = []
    warnings: list[str] = []

    def evaluate(metric: str, value: float, warn_condition, fail_condition) -> None:
        if fail_condition(value):
            statuses[metric] = "FAIL"
            failures.append(metric)
        elif warn_condition(value):
            statuses[metric] = "WARN"
            warnings.append(metric)
        else:
            statuses[metric] = "PASS"

    evaluate("mean_coverage", mean_coverage, lambda x: 20 <= x < 30, lambda x: x < 20)
    evaluate("percent_mapped", percent_mapped, lambda x: 95 <= x < 98, lambda x: x < 95)
    evaluate("breadth_10x", breadth_10x, lambda x: 90 <= x < 95, lambda x: x < 90)
    evaluate(
        "total_length",
        total_length,
        lambda x: 1_000_000 <= x < 1_500_000 or 8_000_000 < x <= 10_000_000,
        lambda x: x < 1_000_000 or x > 10_000_000,
    )
    evaluate("contigs_total", contigs_total, lambda x: 250 < x <= 500, lambda x: x > 500)
    evaluate(
        "largest_contig",
        largest_contig,
        lambda x: 50_000 <= x < 100_000,
        lambda x: x < 50_000,
    )
    evaluate("n50", n50, lambda x: 10_000 <= x < 20_000, lambda x: x < 10_000)

    if gc_percent < 25 or gc_percent > 75:
        statuses["gc_percent"] = "FAIL"
        failures.append("gc_percent")
    else:
        statuses["gc_percent"] = "PASS"

    decision = "FAIL" if failures else ("WARN" if warnings else "PASS")
    return QCResult(
        decision=decision,
        metric_values=metric_values,
        metric_statuses=statuses,
        reasons_fail=[
            QC2_REASON_MESSAGES.get(reason, {}).get("FAIL", reason)
            for reason in failures
        ],
        reasons_warn=[
            QC2_REASON_MESSAGES.get(reason, {}).get("WARN", reason)
            for reason in warnings
        ],
    )


def build_qc2_row(
    sample_id: UUID,
    gate_version: str,
    bbmap_rows: list[dict[str, Any]],
    quast_rows: list[dict[str, Any]],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the persisted QC2 row from BBMap and QUAST results."""

    result = calculate_qc2(bbmap_rows, quast_rows)
    return {
        "qc2_id": qc_id("qc2", sample_id, gate_version),
        "sample_id": sample_id,
        "qc2_decision": result.decision,
        "qc2_gate_version": gate_version,
        "metric_values": result.metric_values,
        "metric_statuses": result.metric_statuses,
        "reasons_fail": result.reasons_fail,
        "reasons_warn": result.reasons_warn,
        "qc_timestamp": now or datetime.now(timezone.utc),
    }
