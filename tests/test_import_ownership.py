from legacy_importer.config import load_config
from legacy_importer.import_ownership import ownership_rows
from legacy_importer.manifest import ManifestRow


def test_ownership_uses_finalized_manifest_fields_and_visibility():
    row = ManifestRow(
        sample_name="S1",
        full_path="/data/S1",
        organization_code="mhh",
        legacy_batch_code="B1",
        visibility="public",
    )
    rows = ownership_rows(row, load_config("configs/legacy_import.yaml"))

    assert rows["organizations"]["organization_code"] == "mhh"
    assert rows["batches"]["batch_accession"] == "legacy-mhh-B1"
    assert rows["runs"]["run_accession"] == "legacy-mhh-B1"
    assert rows["samples"]["visibility"] == "public"
    assert rows["samples"]["created_at"] is not None
    assert "level_2" not in rows["samples"]["source_metadata"]
