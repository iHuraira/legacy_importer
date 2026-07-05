from pathlib import Path

from legacy_importer.config import load_config
from legacy_importer.discovery import discover_gcp_inputs, discover_sample


def test_discovery_finds_reads_and_configured_outputs(tmp_path: Path):
    root = tmp_path / "S1"
    (root / "rawdata").mkdir(parents=True)
    (root / "fastqc").mkdir()
    (root / "logs").mkdir()
    (root / "rawdata" / "S1_R1.fastq.gz").write_bytes(b"r1")
    (root / "rawdata" / "S1_R2.fastq.gz").write_bytes(b"r2")
    (root / "fastqc" / "S1_R1_fastqc.zip").write_bytes(b"zip")
    (root / "S1_quast_report.tsv").write_text("metric\tvalue\n", encoding="utf-8")
    (root / "logs" / "bbmap_S1.log").write_text("BBMap output\n", encoding="utf-8")
    (root / "S1_quality_report.html").write_text("<html></html>", encoding="utf-8")
    inventory = discover_sample(root, load_config("configs/legacy_import.yaml"))
    assert set(inventory.reads) == {"R1", "R2"}
    assert len(inventory.tools["fastqc"]) == 1
    assert any(path.suffix == ".log" for path in inventory.tools["bbmap"])
    assert len(inventory.tools["quast"]) == 1
    assert len(inventory.supplemental["quality_report"]) == 1
    assert inventory.missing == []


def test_discovery_reports_missing_directory(tmp_path: Path):
    inventory = discover_sample(tmp_path / "absent", load_config("configs/legacy_import.yaml"))
    assert inventory.missing == ["sample_directory"]


def test_discovery_accepts_legacy_txt_reads(tmp_path: Path):
    root = tmp_path / "S1"
    (root / "rawdata").mkdir(parents=True)
    (root / "rawdata" / "S1_R1.txt").write_bytes(b"r1")
    (root / "rawdata" / "S1_R2.txt").write_bytes(b"r2")

    inventory = discover_sample(root, load_config("configs/legacy_import.yaml"))

    assert inventory.reads["R1"].name == "S1_R1.txt"
    assert inventory.reads["R2"].name == "S1_R2.txt"
    assert inventory.missing == []


def test_all_prokka_outputs_are_selected_for_gcp_artifact(tmp_path: Path):
    root = tmp_path / "S1"
    prokka = root / "prokka"
    prokka.mkdir(parents=True)
    gbk = prokka / "S1.gbk"
    gff = prokka / "S1.gff"
    gbk.write_text("gbk", encoding="utf-8")
    gff.write_text("gff", encoding="utf-8")
    config = load_config("configs/legacy_import.yaml")

    assert discover_gcp_inputs(root, config, "prokka") == [gbk, gff]
    assert discover_gcp_inputs(root, config, "fastqc") == []


def test_mash_and_ska_sketches_are_selected_for_gcp(tmp_path: Path):
    root = tmp_path / "S1"
    mash_dir = root / "mash_k21_s1000"
    ska_dir = root / "ska_k15"
    mash_dir.mkdir(parents=True)
    ska_dir.mkdir()
    mash = mash_dir / "S1.msh"
    ska = ska_dir / "S1.skf"
    mash.write_bytes(b"mash")
    ska.write_bytes(b"ska")
    config = load_config("configs/legacy_import.yaml")

    assert discover_gcp_inputs(root, config, "mash") == [mash]
    assert discover_gcp_inputs(root, config, "ska") == [ska]
