import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from legacy_importer.import_results import (
    ExtractorError,
    import_tool_results,
    parse_amrfinder,
    parse_bbmap,
    parse_bracken,
    parse_fastqc,
    parse_mlst,
    parse_prokka,
)
from uuid import UUID


def install_fake_extractor(monkeypatch, callback):
    monkeypatch.setitem(
        sys.modules,
        "bio_extractors",
        SimpleNamespace(run_extractor=callback),
    )


def test_fastqc_zip_is_unpacked_and_metrics_are_normalized(tmp_path: Path, monkeypatch):
    archive_path = tmp_path / "S1_R1_fastqc.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("S1_R1_fastqc/fastqc_data.txt", "FastQC data")

    calls = []

    def run_extractor(tool, path):
        calls.append((tool, Path(path).name, Path(path).read_text(encoding="utf-8")))
        return {
            "total_sequences": 100,
            "total_deduplicated_percentage": 0.25,
            "n_content": 0.01,
            "gc_content": 0.52,
        }

    install_fake_extractor(monkeypatch, run_extractor)
    row = parse_fastqc([archive_path])[0]

    assert calls == [("fastqc", "fastqc_data.txt", "FastQC data")]
    assert row["total_reads"] == 100
    assert row["duplication_rate"] == 0.25
    assert row["n_content"] == 0.01
    assert row["gc_content"] == 0.52
    assert row["_source_path"] == str(archive_path)


def test_fastqc_rejects_zip_without_data_file(tmp_path: Path):
    archive_path = tmp_path / "bad_fastqc.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("summary.txt", "missing")
    with pytest.raises(ExtractorError, match="exactly one fastqc_data.txt"):
        parse_fastqc([archive_path])


def test_adapters_select_only_supported_files(tmp_path: Path, monkeypatch):
    stats = tmp_path / "S1_bb_stats.txt"
    histogram = tmp_path / "S1_bb_hist.txt"
    log = tmp_path / "bbmap_S1.log"
    gff = tmp_path / "S1.gff"
    faa = tmp_path / "S1.faa"
    amr_report = tmp_path / "S1_amrfinder_report.tsv"
    organism = tmp_path / "S1_organism.txt"
    for path in (stats, histogram, log, gff, faa, amr_report, organism):
        path.write_text("data", encoding="utf-8")

    calls = []

    def run_extractor(tool, path):
        calls.append((tool, Path(path).name))
        return {"mapped_percent": 91.5} if tool == "bbmap" else {"gene": "x"}

    install_fake_extractor(monkeypatch, run_extractor)

    assert parse_bbmap([stats, histogram, log])[0]["percent_mapped"] == 91.5
    parse_prokka([gff, faa])
    parse_amrfinder([amr_report, organism])
    assert calls == [
        ("bbmap", "bbmap_S1.log"),
        ("prokka", "S1.gff"),
        ("amrfinder", "S1_amrfinder_report.tsv"),
    ]


def test_missing_package_has_actionable_error(monkeypatch):
    real_import = __import__("importlib").import_module

    def missing(name):
        if name == "bio_extractors":
            raise ModuleNotFoundError(name)
        return real_import(name)

    monkeypatch.setattr("legacy_importer.import_results.importlib.import_module", missing)
    with pytest.raises(ExtractorError, match="bio-extractors"):
        parse_prokka([Path("sample.gff")])


def test_database_field_aliases_are_added(tmp_path: Path, monkeypatch):
    mlst = tmp_path / "sample_mlst.txt"
    bracken = tmp_path / "sample_kraken_report_bracken.txt"
    mlst.write_text("data", encoding="utf-8")
    bracken.write_text("data", encoding="utf-8")

    def run_extractor(tool, _path):
        if tool == "mlst":
            return {"species": "kpneumoniae", "st_type": 1}
        return {
            "D": "Bacteria",
            "P": "Pseudomonadota",
            "C": "Gammaproteobacteria",
            "O": "Enterobacterales",
            "F": "Enterobacteriaceae",
            "G": "Klebsiella",
            "G_taxonomy_id": 570,
            "g_percent_sum": 99.0,
        }

    install_fake_extractor(monkeypatch, run_extractor)

    assert parse_mlst([mlst])[0]["mlst_species"] == "kpneumoniae"
    row = parse_bracken([bracken])[0]
    assert row["domain"] == "Bacteria"
    assert row["genus"] == "Klebsiella"
    assert row["genus_taxonomy_id"] == 570
    assert row["genus_percent_sum"] == 99.0


def test_multirow_results_are_sent_to_one_bulk_upsert(monkeypatch):
    captured = []
    monkeypatch.setitem(
        __import__("legacy_importer.import_results", fromlist=["PARSERS"]).PARSERS,
        "prokka",
        lambda _paths: [
            {"locus_tag": "A", "gene": "a"},
            {"locus_tag": "B", "gene": "b"},
        ],
    )
    monkeypatch.setattr(
        "legacy_importer.import_results.bulk_upsert",
        lambda _connection, table, rows, conflicts: captured.append(
            (table, rows, conflicts)
        ),
    )

    rows = import_tool_results(
        object(),
        "prokka",
        [Path("sample.gff")],
        UUID("10000000-0000-0000-0000-000000000001"),
        UUID("20000000-0000-0000-0000-000000000001"),
        {},
        "bakta_annotations",
    )

    assert len(rows) == 2
    assert len(captured) == 1
    assert captured[0][0] == "bakta_annotations"
    assert captured[0][2] == ["bakta_annotation_id"]
