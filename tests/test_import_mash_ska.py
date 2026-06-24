from datetime import datetime, timezone
from uuid import UUID

import pytest

from legacy_importer import ids
from legacy_importer.config import SkaSampleOverrideConfig, load_config
from legacy_importer.import_mash_ska import (
    MissingSamplesError,
    SampleRecord,
    add_unique_ska_distance,
    apply_ska_sample_overrides,
    batch_artifact_object_name,
    build_sample_index,
    build_ska_distance_row,
    count_ska_csv_rows,
    ensure_global_context,
    global_context_rows,
    insert_mash_master_samples,
    load_ska_checkpoint,
    map_sample_names,
    mash_membership_records,
    save_ska_checkpoint,
    ska_checkpoint_path,
    ska_csv_sample_names,
)


def sample(number: int, name: str) -> SampleRecord:
    return SampleRecord(
        sample_id=UUID(f"00000000-0000-0000-0000-{number:012d}"),
        organization_id=UUID("10000000-0000-0000-0000-000000000001"),
        sample_name=name,
        sample_accession=name,
    )


def test_mash_ska_ids_are_deterministic():
    batch = ids.global_batch_id()
    mash = ids.mash_master_id(batch, 1)
    ska = ids.ska_master_id(batch, 1)
    first = sample(1, "S1")
    second = sample(2, "S2")

    assert mash == ids.mash_master_id(batch, 1)
    assert ska == ids.ska_master_id(batch, 1)
    assert ids.ska_distance_id(ska, first.sample_id, second.sample_id) == (
        ids.ska_distance_id(ska, second.sample_id, first.sample_id)
    )
    assert mash != ska


def test_global_context_uses_deterministic_ids_and_accessions():
    config = load_config("configs/legacy_import.yaml")
    rows = global_context_rows(config, datetime.now(timezone.utc))

    assert rows["batches"]["batch_id"] == ids.global_batch_id()
    assert rows["batches"]["batch_accession"] == "LEGACY-GLOBAL-BATCH"
    assert rows["batches"]["batch_name"] == "Legacy Global Import"
    assert rows["batches"]["source"] == "legacy_import"
    assert rows["batches"]["finished_at"] is not None
    assert rows["runs"]["run_id"] == ids.global_run_id(ids.global_batch_id())
    assert rows["runs"]["run_accession"] == "LEGACY-GLOBAL-RUN"
    assert rows["runs"]["source"] == "legacy_import"


def test_global_context_reuses_existing_batch_and_run(monkeypatch):
    config = load_config("configs/legacy_import.yaml")
    batch_id = UUID("50000000-0000-0000-0000-000000000001")
    run_id = UUID("60000000-0000-0000-0000-000000000001")
    inserted = []

    class Cursor:
        def __init__(self):
            self.result = None

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def execute(self, query, _params):
            self.result = (
                (batch_id,)
                if "FROM batches" in query
                else (run_id, batch_id)
            )

        def fetchone(self):
            return self.result

    class Connection:
        def cursor(self):
            return Cursor()

    monkeypatch.setattr(
        "legacy_importer.import_mash_ska.upsert",
        lambda _connection, table, row, conflicts: inserted.append(
            (table, row, conflicts)
        ),
    )

    actual = ensure_global_context(Connection(), config)

    assert actual == (batch_id, run_id)
    assert [table for table, _, _ in inserted] == [
        "organizations", "users", "batches", "jobs", "runs"
    ]
    assert inserted[-1][1]["run_id"] == run_id
    assert inserted[-1][1]["batch_id"] == batch_id


def test_mash_artifact_uses_batch_path_convention():
    run = UUID("20000000-0000-0000-0000-000000000001")
    task = UUID("30000000-0000-0000-0000-000000000001")
    assert batch_artifact_object_name(run, task) == f"{run}/batch/{task}.tar.gz"


def test_mash_master_samples_insert_mapping(monkeypatch):
    captured = {}

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

    class Connection:
        def cursor(self):
            return Cursor()

    def fake_execute_values(_cursor, query, rows, page_size):
        captured["query"] = query
        captured["rows"] = rows
        captured["page_size"] = page_size

    monkeypatch.setattr(
        "legacy_importer.import_mash_ska.execute_values",
        fake_execute_values,
    )
    master = UUID("40000000-0000-0000-0000-000000000001")
    added_at = datetime.now(timezone.utc)
    records = [sample(1, "S1"), sample(2, "S2")]

    assert insert_mash_master_samples(
        Connection(),
        master,
        records,
        added_at,
    ) == 2
    assert captured["rows"] == [
        (str(master), str(records[0].sample_id), str(records[0].organization_id), added_at),
        (str(master), str(records[1].sample_id), str(records[1].organization_id), added_at),
    ]
    assert "ON CONFLICT (mash_master_id, sample_id)" in captured["query"]


def test_ska_csv_names_map_to_sample_ids():
    first = sample(1, "S1")
    second = sample(2, "S2")
    index, ambiguous = build_sample_index([first, second])

    assert {row.sample_id for row in map_sample_names(
        ["S2", "S1"],
        index,
        ambiguous,
    )} == {first.sample_id, second.sample_id}


def test_ska_sample_override_resolves_ambiguous_name():
    config = load_config("configs/legacy_import.yaml")
    first = SampleRecord(
        ids.sample_id("mhh", "B1", "same"),
        UUID("10000000-0000-0000-0000-000000000001"),
        "same",
        "same",
    )
    second = SampleRecord(
        ids.sample_id("mhh", "B2", "same"),
        UUID("10000000-0000-0000-0000-000000000001"),
        "same",
        "same",
    )
    config.mash_ska.ska_sample_overrides = {
        "same": SkaSampleOverrideConfig(
            organization_code="mhh",
            legacy_batch_code="B2",
            sample_name="same",
        )
    }
    index, ambiguous = build_sample_index([first, second])

    apply_ska_sample_overrides(config, [first, second], index, ambiguous)

    assert index["same"].sample_id == second.sample_id
    assert "same" not in ambiguous


def test_mash_manifest_membership_uses_deterministic_sample_ids(
    tmp_path,
    monkeypatch,
):
    config = load_config("configs/legacy_import.yaml")
    manifest = tmp_path / "manifest.csv"
    manifest.write_text(
        "sample_name,full_path,organization_code,legacy_batch_code,visibility\n"
        "same,/data/one,mhh,B1,private\n"
        "same,/data/two,mhh,B2,private\n",
        encoding="utf-8",
    )
    config.mash_ska.sample_source.manifest_csv = manifest
    records = [
        SampleRecord(
            ids.sample_id("mhh", "B1", "same"),
            UUID("10000000-0000-0000-0000-000000000001"),
            "same",
            "same",
        ),
        SampleRecord(
            ids.sample_id("mhh", "B2", "same"),
            UUID("10000000-0000-0000-0000-000000000001"),
            "same",
            "same",
        ),
    ]
    monkeypatch.setattr(
        "legacy_importer.import_mash_ska.load_sample_records",
        lambda *_: records,
    )

    assert {record.sample_id for record in mash_membership_records(None, config)} == {
        record.sample_id for record in records
    }


def test_duplicate_reciprocal_ska_pair_is_prevented():
    config = load_config("configs/legacy_import.yaml")
    master = ids.ska_master_id(ids.global_batch_id(), 1)
    first = sample(1, "S1")
    second = sample(2, "S2")
    now = datetime.now(timezone.utc)
    forward = build_ska_distance_row(
        config, master, first, second, "12", None, now
    )
    reverse = build_ska_distance_row(
        config, master, second, first, "12", None, now
    )
    seen = set()
    pending = []

    assert add_unique_ska_distance(forward, seen, pending) is True
    assert add_unique_ska_distance(reverse, seen, pending) is False
    assert len(pending) == 1
    assert pending[0]["category"] == "close"


def test_missing_sample_report_generation():
    index, ambiguous = build_sample_index([sample(1, "S1")])

    with pytest.raises(MissingSamplesError) as error:
        map_sample_names(["S1", "missing"], index, ambiguous)

    assert error.value.report() == {
        "ok": False,
        "missing_count": 1,
        "missing_samples": ["missing"],
        "ambiguous_count": 0,
        "ambiguous_samples": [],
    }


def test_ska_csv_progress_reports_rows(tmp_path):
    config = load_config("configs/legacy_import.yaml")
    csv_path = tmp_path / "distances.csv"
    csv_path.write_text(
        "sample1,sample2,SNPs\nS1,S2,1\nS2,S1,1",
        encoding="utf-8",
    )
    config.mash_ska.ska_distances_csv = csv_path
    updates = []

    total = count_ska_csv_rows(config)
    names = ska_csv_sample_names(
        config,
        lambda label, current, maximum: updates.append(
            (label, current, maximum)
        ),
        total,
    )

    assert total == 2
    assert names == {"S1", "S2"}
    assert updates[-1] == ("SKA: mapping samples", 2, 2)


def test_ska_checkpoint_resumes_only_matching_input(tmp_path):
    config = load_config("configs/legacy_import.yaml")
    config.mash_ska.checkpoint_dir = tmp_path / "state"
    csv_path = tmp_path / "distances.csv"
    csv_path.write_text("sample1,sample2,SNPs\nS1,S2,1\n", encoding="utf-8")
    master = ids.ska_master_id(ids.global_batch_id(), 1)

    path = save_ska_checkpoint(config, master, csv_path, 1)

    assert path == ska_checkpoint_path(config, master)
    assert load_ska_checkpoint(config, master, csv_path) == 1
    csv_path.write_text(
        "sample1,sample2,SNPs\nS1,S2,1\nS2,S3,2\n",
        encoding="utf-8",
    )
    assert load_ska_checkpoint(config, master, csv_path) == 0
