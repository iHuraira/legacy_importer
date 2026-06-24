from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from legacy_importer.config import load_config
from legacy_importer.import_artifacts import import_artifact, task_row


def test_task_row_includes_organization_owner():
    organization_id = UUID("10000000-0000-0000-0000-000000000001")
    row = task_row(
        load_config("configs/legacy_import.yaml"),
        UUID("20000000-0000-0000-0000-000000000001"),
        UUID("30000000-0000-0000-0000-000000000001"),
        organization_id,
        "fastqc",
        datetime.now(timezone.utc),
    )

    assert row["organization_id"] == organization_id


def test_artifact_row_includes_organization_owner(tmp_path: Path, monkeypatch):
    config = load_config("configs/legacy_import.yaml")
    sample_id = UUID("30000000-0000-0000-0000-000000000001")
    organization_id = UUID("10000000-0000-0000-0000-000000000001")
    source = tmp_path / "report.txt"
    source.write_text("report", encoding="utf-8")
    inserted = []

    monkeypatch.setenv("TMPDIR", str(tmp_path.parent))
    monkeypatch.setattr(
        "legacy_importer.import_artifacts.upsert",
        lambda _connection, table, row, _conflicts: inserted.append((table, row)),
    )
    monkeypatch.setattr(
        "legacy_importer.import_artifacts.upload_file",
        lambda *_: {
            "uri": "gs://bucket/object",
            "size_bytes": 10,
            "checksum": "checksum",
        },
    )

    _, artifact, archive = import_artifact(
        None,
        object(),
        config,
        tmp_path,
        UUID("20000000-0000-0000-0000-000000000001"),
        sample_id,
        organization_id,
        "fastqc",
        [source],
    )
    archive.unlink()

    assert artifact["organization_id"] == organization_id
    assert inserted[-1] == ("artifacts", artifact)
