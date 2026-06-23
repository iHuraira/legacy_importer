from pathlib import Path

from legacy_importer.export_summary import (
    SUMMARY_FIELDS,
    _write_summary_csv,
    build_summary_row,
)


def test_summary_is_built_from_normalized_results():
    summary = build_summary_row(
        {
            "sample_id": "sample-id",
            "sample_name": "S1",
            "batch_id": "batch-id",
            "created_at": "2026-06-22",
        },
        {
            "bracken": [
                {
                    "domain": "Bacteria",
                    "genus": "Klebsiella",
                    "top_species_name": "Klebsiella pneumoniae",
                    "top_species_percent": 80,
                    "genus_percent_sum": 81,
                }
            ],
            "mlst": [
                {
                    "mlst_species": "kpneumoniae",
                    "st_type": 1,
                    "allelic_profile": ["a(1)", "b(2)"],
                }
            ],
            "quast": [
                {
                    "total_length": 5_000_000,
                    "gc_percent": 57,
                    "contigs_total": 40,
                    "n50": 100_000,
                    "l50": 10,
                }
            ],
            "bbmap": [{"avg_coverage": 100, "percent_mapped": 99}],
            "prokka": [{"gene": "a"}, {"gene": "b"}],
            "amrfinder": [{"gene": "oqxA"}, {"gene": "fosA"}],
        },
        {"qc1_decision": "WARN"},
        {"qc2_decision": "PASS"},
    )

    assert summary["tax_tree"] == "Bacteria/Klebsiella/Klebsiella pneumoniae"
    assert summary["allelic_profile"] == "a(1);b(2)"
    assert summary["gene_count"] == 2
    assert summary["amr_gene_count"] == 2
    assert summary["top_amr_gene"] == "oqxA"
    assert summary["overall_qc"] == "WARN"


def test_summary_csv_is_written_with_stable_headers(tmp_path: Path):
    summary = {field: None for field in SUMMARY_FIELDS}
    summary["sample_id"] = "sample-id"
    summary["overall_qc"] = "PASS"
    output = _write_summary_csv(summary, "S1", tmp_path)
    lines = output.read_text(encoding="utf-8").splitlines()
    assert lines[0] == ",".join(SUMMARY_FIELDS)
    assert lines[1].startswith("sample-id,")
    assert lines[1].endswith(",PASS")
