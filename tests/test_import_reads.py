from pathlib import Path

from legacy_importer.config import load_config
from legacy_importer.import_reads import import_reads, raw_read_object_name
from legacy_importer.manifest import ManifestRow


def test_raw_reads_are_uploaded_directly(tmp_path: Path, monkeypatch):
    sample_dir = tmp_path / "S1"
    rawdata = sample_dir / "rawdata"
    rawdata.mkdir(parents=True)
    read_path = rawdata / "S1_R1.fastq.gz"
    read_path.write_bytes(b"read-data")
    row = ManifestRow(
        sample_name="S1",
        full_path=sample_dir,
        organization_code="mhh",
        legacy_batch_code="B1",
    )
    config = load_config("configs/legacy_import.yaml")
    captured = {}

    def fake_upload(_client, bucket, object_name, upload_path):
        captured["bucket"] = bucket
        captured["object_name"] = object_name
        captured["upload_path"] = upload_path
        return {
            "uri": f"gs://{bucket}/{object_name}",
            "size_bytes": upload_path.stat().st_size,
            "checksum": "gcs-checksum",
        }

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr("legacy_importer.import_reads.upload_file", fake_upload)
    monkeypatch.setattr("legacy_importer.import_reads.upsert", lambda *_: None)

    imported = import_reads(
        None,
        object(),
        config,
        row,
        "11111111-1111-1111-1111-111111111111",
        {"R1": read_path},
    )

    assert captured["object_name"] == (
        "legacy_import/raw_reads/mhh/B1/S1/S1_R1.fastq.gz"
    )
    assert captured["upload_path"] == read_path
    assert imported["R1"]["original_filename"] == "S1_R1.fastq.gz"
    assert imported["R1"]["uri"].endswith(".fastq.gz")


def test_raw_read_object_name_is_stable():
    config = load_config("configs/legacy_import.yaml")
    row = ManifestRow(
        sample_name="S1",
        full_path="/data/S1",
        organization_code="mhh",
        legacy_batch_code="B1",
    )
    assert raw_read_object_name(config, row, "S1_R2.fastq.gz") == (
        "legacy_import/raw_reads/mhh/B1/S1/S1_R2.fastq.gz"
    )
