from pathlib import Path

import pytest

from legacy_importer.cli import _protect_source_paths
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
