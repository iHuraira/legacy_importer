import tarfile
from pathlib import Path

import pytest

from legacy_importer.archive import create_archive, sha256_file


def test_archive_preserves_paths_below_output(tmp_path: Path):
    sample = tmp_path / "sample"
    nested = sample / "prokka"
    nested.mkdir(parents=True)
    first = sample / "report.txt"
    second = nested / "sample.gff"
    first.write_text("report", encoding="utf-8")
    second.write_text("gff", encoding="utf-8")
    archive = create_archive(sample, [second, first], "artifact", tmp_path)
    with tarfile.open(archive, "r:gz") as handle:
        assert handle.getnames() == ["output/report.txt", "output/prokka/sample.gff"]
    assert len(sha256_file(archive)) == 64


def test_archive_rejects_files_outside_sample(tmp_path: Path):
    sample = tmp_path / "sample"
    sample.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("no", encoding="utf-8")
    with pytest.raises(ValueError, match="below"):
        create_archive(sample, [outside], "artifact", tmp_path)


def test_archive_rejects_output_inside_source_data(tmp_path: Path):
    sample = tmp_path / "sample"
    sample.mkdir()
    source = sample / "report.txt"
    source.write_text("data", encoding="utf-8")
    with pytest.raises(ValueError, match="outside source data"):
        create_archive(sample, [source], "artifact", sample)


def test_archive_compression_level_is_configurable_and_reproducible(tmp_path: Path):
    sample = tmp_path / "sample"
    sample.mkdir()
    source = sample / "repeated.txt"
    source.write_bytes(b"repeated-data-" * 10000)

    fast = create_archive(
        sample,
        [source],
        "fast",
        tmp_path,
        compression_level=1,
    )
    dense = create_archive(
        sample,
        [source],
        "dense",
        tmp_path,
        compression_level=9,
    )

    with tarfile.open(fast, "r:gz") as handle:
        assert handle.extractfile("output/repeated.txt").read() == source.read_bytes()
    assert dense.stat().st_size <= fast.stat().st_size


def test_archive_rejects_invalid_compression_level(tmp_path: Path):
    sample = tmp_path / "sample"
    sample.mkdir()
    source = sample / "report.txt"
    source.write_text("report", encoding="utf-8")

    with pytest.raises(ValueError, match="between 0 and 9"):
        create_archive(sample, [source], "artifact", tmp_path, compression_level=10)
