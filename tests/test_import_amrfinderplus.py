from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest

from legacy_importer.import_amrfinderplus import (
    build_amrfinderplus_rows,
    parse_amrfinderplus_tsv,
    resolve_existing_amrfinder_task,
)


HEADERS = [
    "Name", "Protein identifier", "Contig id", "Start", "Stop", "Strand",
    "Gene symbol", "Sequence name", "Scope", "Element type", "Element subtype",
    "Class", "Subclass", "Method", "Target length",
    "Reference sequence length", "% Coverage of reference sequence",
    "% Identity to reference sequence", "Alignment length",
    "Accession of closest sequence", "Name of closest sequence", "HMM id",
    "HMM description",
]


def test_full_amrfinderplus_report_is_parsed(tmp_path: Path):
    report = tmp_path / "S1_amrfinder_report.tsv"
    values = [
        "S1", "S1_00034", "contig-1", "30771", "31946", "+", "oqxA",
        "multidrug efflux protein", "core", "AMR", "AMR",
        "PHENICOL/QUINOLONE", "PHENICOL/QUINOLONE", "BLASTP", "391", "391",
        "100.00", "98.98", "391", "WP_002914189.1", "OqxA", "NF000272.1",
        "OqxA HMM",
    ]
    report.write_text(
        "\t".join(HEADERS) + "\n" + "\t".join(values) + "\n",
        encoding="utf-8",
    )

    rows = parse_amrfinderplus_tsv(report)

    assert rows == [{
        "name": "S1",
        "protein_identifier": "S1_00034",
        "contig_id": "contig-1",
        "start_position": 30771,
        "stop_position": 31946,
        "strand": "+",
        "gene_symbol": "oqxA",
        "sequence_name": "multidrug efflux protein",
        "scope": "core",
        "element_type": "AMR",
        "element_subtype": "AMR",
        "class": "PHENICOL/QUINOLONE",
        "subclass": "PHENICOL/QUINOLONE",
        "method": "BLASTP",
        "target_length": 391,
        "reference_sequence_length": 391,
        "reference_coverage_percent": 100.0,
        "reference_identity_percent": 98.98,
        "alignment_length": 391,
        "closest_sequence_accession": "WP_002914189.1",
        "closest_sequence_name": "OqxA",
        "hmm_id": "NF000272.1",
        "hmm_description": "OqxA HMM",
    }]


def test_report_requires_all_expected_columns(tmp_path: Path):
    report = tmp_path / "bad.tsv"
    report.write_text("Name\tProtein identifier\nS1\tP1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="missing AMRFinderPlus columns"):
        parse_amrfinderplus_tsv(report)


def test_rows_use_deterministic_amrfinderplus_ids():
    sample_id = UUID("00000000-0000-0000-0000-000000000001")
    task_id = UUID("00000000-0000-0000-0000-000000000002")
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    parsed = [{
        "protein_identifier": "P1",
        "contig_id": "C1",
        "start_position": 1,
        "stop_position": 10,
        "method": "BLASTP",
        "gene_symbol": "gene",
    }]

    first = build_amrfinderplus_rows(
        parsed, sample_id, task_id, Path("report.tsv"), created_at=timestamp
    )
    second = build_amrfinderplus_rows(
        parsed, sample_id, task_id, Path("report.tsv"), created_at=timestamp
    )

    assert first == second
    assert first[0]["sample_id"] == sample_id
    assert first[0]["task_id"] == task_id
    assert first[0]["amrfinderplus_id"]


def test_existing_amrfinder_task_must_be_unambiguous():
    task_id = UUID("00000000-0000-0000-0000-000000000002")

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, _query, params):
            assert params[1] == "amrfinder"

        def fetchall(self):
            return [(task_id,)]

    class Connection:
        def cursor(self):
            return Cursor()

    assert resolve_existing_amrfinder_task(
        Connection(), UUID("00000000-0000-0000-0000-000000000001")
    ) == task_id
