from uuid import UUID

import pytest

from legacy_importer.qc import (
    build_qc1_row,
    build_qc2_row,
    build_qc_row,
    calculate_qc1,
    calculate_qc2,
    evaluate_thresholds,
)


def test_qc_pass_fail_and_warn_decisions():
    thresholds = {"min_total_reads": 100, "max_n_content": 10}
    assert evaluate_thresholds({"total_reads": 101, "n_content": 5}, thresholds).decision == "PASS"
    assert evaluate_thresholds({"total_reads": 99, "n_content": 5}, thresholds).decision == "FAIL"
    assert evaluate_thresholds({"total_reads": 101}, thresholds).decision == "WARN"


def test_qc_row_id_is_deterministic():
    sample_id = UUID("11111111-1111-1111-1111-111111111111")
    first = build_qc_row("qc1", sample_id, "v1", {"total_reads": 10}, {"min_total_reads": 1})
    second = build_qc_row("qc1", sample_id, "v1", {"total_reads": 10}, {"min_total_reads": 1})
    assert first["qc1_id"] == second["qc1_id"]
    assert first["qc1_decision"] == "PASS"


def fastqc_row(read_type, total_reads=400_000, read_length=150, **metrics):
    return {
        "read_type": read_type,
        "total_reads": total_reads,
        "read_length": read_length,
        "duplication_rate": metrics.get("duplication_rate", 0.20),
        "n_content": metrics.get("n_content", 0.005),
        "gc_content": metrics.get("gc_content", 0.50),
    }


def test_qc1_calculates_paired_read_metrics_and_passes():
    result = calculate_qc1([fastqc_row("R1"), fastqc_row("R2")])

    assert result.decision == "PASS"
    assert result.metric_values["coverage_small"] == 40
    assert result.metric_values["coverage_big"] == 20
    assert result.metric_values["read_imbalance_ratio"] == 1
    assert set(result.metric_statuses.values()) == {"PASS"}


def test_qc1_uses_worst_pair_metrics_and_reason_messages():
    result = calculate_qc1([
        fastqc_row("R1", total_reads=200_000, duplication_rate=0.35, n_content=0.02),
        fastqc_row("R2", total_reads=210_000, duplication_rate=0.20, n_content=0.005),
    ])

    assert result.decision == "WARN"
    assert result.metric_values["duplication_rate"] == 0.35
    assert result.metric_values["n_content"] == 0.02
    assert result.metric_statuses["coverage_small"] == "WARN"
    assert "Duplicate read levels are higher than expected." in result.reasons_warn


def test_qc1_fails_invalid_read_length_and_severe_metrics():
    result = calculate_qc1([
        fastqc_row("R1", total_reads=100_000, read_length=140, duplication_rate=0.50),
        fastqc_row("R2", total_reads=70_000, read_length=150, gc_content=0.80),
    ])

    assert result.decision == "FAIL"
    assert result.metric_statuses["read_length"] == "FAIL"
    assert result.metric_statuses["duplication_rate"] == "FAIL"
    assert result.metric_statuses["read_imbalance_ratio"] == "FAIL"
    assert "Read length is missing or outside the supported range." in result.reasons_fail


def test_qc1_accepts_legacy_percentage_values_and_maps_read_ids():
    sample_id = UUID("11111111-1111-1111-1111-111111111111")
    rows = [
        {**fastqc_row("R1"), "read_type": None, "read_id": "read-1", "gc_content": 50},
        {**fastqc_row("R2"), "read_type": None, "read_id": "read-2", "gc_content": 50},
    ]
    reads = {"R1": {"read_id": "read-1"}, "R2": {"read_id": "read-2"}}

    row = build_qc1_row(sample_id, "qc_gate_loose_v1", rows, reads)

    assert row["qc1_decision"] == "PASS"
    assert row["metric_values"]["gc_content"] == 0.5


def test_qc1_requires_both_fastqc_rows():
    with pytest.raises(RuntimeError, match="both R1 and R2"):
        calculate_qc1([fastqc_row("R1")])


def bbmap_row(**values):
    return {
        "avg_coverage": values.get("avg_coverage", 30),
        "percent_mapped": values.get("percent_mapped", 98),
        "percent_ref_bases_covered": values.get("percent_ref_bases_covered", 95),
    }


def quast_row(**values):
    return {
        "total_length": values.get("total_length", 5_000_000),
        "contigs_total": values.get("contigs_total", 200),
        "largest_contig": values.get("largest_contig", 100_000),
        "n50": values.get("n50", 20_000),
        "gc_percent": values.get("gc_percent", 50),
    }


def test_qc2_passes_and_normalizes_fractional_gc():
    result = calculate_qc2([bbmap_row()], [quast_row(gc_percent=0.5)])

    assert result.decision == "PASS"
    assert result.metric_values["gc_percent"] == 50
    assert set(result.metric_statuses.values()) == {"PASS"}


def test_qc2_warns_at_intermediate_thresholds():
    result = calculate_qc2(
        [bbmap_row(avg_coverage=25, percent_mapped=96, percent_ref_bases_covered=92)],
        [quast_row(total_length=1_200_000, contigs_total=300, largest_contig=75_000, n50=15_000)],
    )

    assert result.decision == "WARN"
    assert all(status == "WARN" for name, status in result.metric_statuses.items() if name != "gc_percent")
    assert "Mean coverage is lower than recommended." in result.reasons_warn


def test_qc2_fails_severe_metrics_with_reason_messages():
    result = calculate_qc2(
        [bbmap_row(avg_coverage=19, percent_mapped=94, percent_ref_bases_covered=89)],
        [quast_row(total_length=900_000, contigs_total=501, largest_contig=49_999, n50=9_999, gc_percent=80)],
    )

    assert result.decision == "FAIL"
    assert set(result.metric_statuses.values()) == {"FAIL"}
    assert "Assembly GC content is outside the expected range." in result.reasons_fail


def test_qc2_requires_both_sources_and_numeric_fields():
    with pytest.raises(RuntimeError, match="BBMap"):
        calculate_qc2([], [quast_row()])
    with pytest.raises(RuntimeError, match="QUAST"):
        calculate_qc2([bbmap_row()], [])
    with pytest.raises(RuntimeError, match="BBMap.avg_coverage"):
        calculate_qc2([{**bbmap_row(), "avg_coverage": None}], [quast_row()])


def test_qc2_row_is_deterministic():
    sample_id = UUID("11111111-1111-1111-1111-111111111111")
    first = build_qc2_row(sample_id, "qc2_rules_v1", [bbmap_row()], [quast_row()])
    second = build_qc2_row(sample_id, "qc2_rules_v1", [bbmap_row()], [quast_row()])
    assert first["qc2_id"] == second["qc2_id"]
    assert first["qc2_decision"] == "PASS"
