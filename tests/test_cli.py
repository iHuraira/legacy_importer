from pathlib import Path

import pytest

import json

from legacy_importer.cli import (
    _append_failure_report,
    _emit_timing_breakdown,
    _import_one_sample,
    _load_existing_sample_ids,
    _protect_source_paths,
)
from legacy_importer.config import load_config
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


def test_sample_import_reconnects_after_transient_database_error(monkeypatch):
    connections = [object(), object()]
    calls = []
    row = ManifestRow(
        sample_name="S1",
        full_path="/data/S1",
        organization_code="mhh",
        legacy_batch_code="B1",
    )

    monkeypatch.setattr(
        "legacy_importer.cli.connect",
        lambda _settings: connections.pop(0),
    )
    monkeypatch.setattr("legacy_importer.cli.safe_rollback", lambda connection: None)
    monkeypatch.setattr(
        "legacy_importer.cli.safe_close",
        lambda connection: calls.append(("close", connection)),
    )
    monkeypatch.setattr("legacy_importer.cli.sleep", lambda _seconds: None)

    def fake_import(connection, *_args, **_kwargs):
        calls.append(("import", connection))
        if len([call for call in calls if call[0] == "import"]) == 1:
            import psycopg2

            raise psycopg2.InterfaceError("connection already closed")
        return {"sample": "S1"}

    monkeypatch.setattr("legacy_importer.cli.import_sample", fake_import)

    status, report, attempts = _import_one_sample(
        load_config("configs/legacy_import.yaml"),
        row,
        "sample-id",
        skip_existing=False,
        skip_reads=False,
        skip_artifacts=False,
        skip_extractors=False,
        skip_qc=False,
        database_retries=2,
        retry_base_seconds=0,
    )

    assert status == "imported"
    assert report == {"sample": "S1"}
    assert attempts == 2
    assert len([call for call in calls if call[0] == "close"]) == 2


def test_timing_breakdown_adds_untracked_overhead(capsys):
    result = _emit_timing_breakdown(
        "S1",
        {"gcs_upload": 6.0, "database_write": 1.0},
        10.0,
    )

    output = capsys.readouterr().out
    assert "gcs_upload: 6.0s" in output
    assert "database_write: 1.0s" in output
    assert result["untracked_overhead"] == 3.0


def test_existing_samples_are_loaded_in_chunks_with_one_connection(monkeypatch):
    queried = []
    closed = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def execute(self, _query, params):
            queried.append(params[0])

        def fetchall(self):
            return [(queried[-1][0],)]

    class Connection:
        closed = 0

        def cursor(self):
            return Cursor()

    connection = Connection()
    monkeypatch.setattr("legacy_importer.cli.connect", lambda _settings: connection)
    monkeypatch.setattr(
        "legacy_importer.cli.safe_close",
        lambda value: closed.append(value),
    )

    existing = _load_existing_sample_ids(
        load_config("configs/legacy_import.yaml"),
        ["1", "2", "3"],
        database_retries=0,
        retry_base_seconds=0,
        chunk_size=2,
    )

    assert queried == [["1", "2"], ["3"]]
    assert existing == {"1", "3"}
    assert closed == [connection]
