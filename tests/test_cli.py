from pathlib import Path

import pytest

import json

from legacy_importer.cli import _append_failure_report, _protect_source_paths
from legacy_importer.manifest import ManifestRow


def test_generated_output_is_rejected_inside_manifest_source(tmp_path: Path):
    source = tmp_path / "sample"
    source.mkdir()
    row = ManifestRow(
        sample_name="S1",
        full_path=source,
        organization_code="mhh",
        legacy_batch_code="B1",
    )

    with pytest.raises(ValueError, match="inside source data"):
        _protect_source_paths(source / "inventory.csv", [row])


def test_generated_output_is_allowed_outside_manifest_source(tmp_path: Path):
    source = tmp_path / "sample"
    source.mkdir()
    row = ManifestRow(
        sample_name="S1",
        full_path=source,
        organization_code="mhh",
        legacy_batch_code="B1",
    )

    _protect_source_paths(tmp_path / "inventory.csv", [row])


def test_failure_report_is_appended_as_json_lines(tmp_path: Path):
    report = tmp_path / "state" / "failures.jsonl"

    _append_failure_report(report, {"sample": "S1", "error": "failed"})
    _append_failure_report(report, {"sample": "S2", "error": "failed again"})

    rows = [
        json.loads(line)
        for line in report.read_text(encoding="utf-8").splitlines()
    ]
    assert [row["sample"] for row in rows] == ["S1", "S2"]
