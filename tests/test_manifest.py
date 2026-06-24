from pathlib import Path

import pytest

from legacy_importer.manifest import read_manifest


def test_manifest_parses_finalized_six_column_shape(tmp_path: Path):
    manifest = tmp_path / "samples.csv"
    manifest.write_text(
        "sample_name,full_path,organization_code,organization_name,"
        "legacy_batch_code,visibility\n"
        "S1,/data/S1,mhh,Medizinische Hochschule Hannover,B1,private\n",
        encoding="utf-8",
    )
    row = read_manifest(manifest)[0]
    assert row.sample_name == "S1"
    assert row.organization_code == "mhh"
    assert row.organization_name == "Medizinische Hochschule Hannover"
    assert row.legacy_batch_code == "B1"
    assert row.visibility == "private"
    assert row.full_path == Path("/data/S1")
    assert row.source_metadata() == {
        "sample_name": "S1",
        "full_path": str(Path("/data/S1")),
        "organization_code": "mhh",
        "organization_name": "Medizinische Hochschule Hannover",
        "legacy_batch_code": "B1",
        "visibility": "private",
    }


def test_manifest_requires_all_columns(tmp_path: Path):
    manifest = tmp_path / "bad.csv"
    manifest.write_text("sample_name,full_path\nS1,/data/S1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing columns"):
        read_manifest(manifest)
