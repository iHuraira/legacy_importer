"""Bio-extractor adapters, row enrichment, and result insertion."""

from __future__ import annotations

import importlib
import re
import tempfile
import zipfile
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from uuid import UUID

from .db import bulk_upsert
from .ids import result_id
from .timing import PhaseTimer


class ExtractorError(RuntimeError):
    """Raised when an external extractor cannot be loaded or process its input."""


def _run_extractor(tool_name: str, input_path: Path, **kwargs: Any) -> list[dict[str, Any]]:
    """Load bio-extractors lazily and normalize its dictionary/list return shape."""

    try:
        module = importlib.import_module("bio_extractors")
        run_extractor: Callable[..., Any] = getattr(module, "run_extractor")
    except (ImportError, AttributeError) as exc:
        raise ExtractorError(
            "Extractor integration requires bio-extractors and its public "
            "bio_extractors.run_extractor function."
        ) from exc

    try:
        result = run_extractor(tool_name, str(input_path), **kwargs)
    except Exception as exc:
        raise ExtractorError(
            f"{tool_name} extractor failed for {input_path}: {exc}"
        ) from exc

    if result is None:
        return []
    rows = result if isinstance(result, list) else [result]
    if not all(isinstance(row, dict) for row in rows):
        raise ExtractorError(
            f"{tool_name} extractor returned {type(result).__name__}; "
            "expected a dictionary or list of dictionaries."
        )
    return [dict(row) for row in rows]


def _as_paths(paths: Path | Iterable[Path]) -> list[Path]:
    if isinstance(paths, (str, Path)):
        return [Path(paths)]
    return [Path(path) for path in paths]


def _select_paths(paths: Path | Iterable[Path], suffixes: tuple[str, ...]) -> list[Path]:
    return [
        path
        for path in _as_paths(paths)
        if path.name.lower().endswith(suffixes)
    ]


def _with_source(rows: list[dict[str, Any]], source: Path) -> list[dict[str, Any]]:
    for row in rows:
        row["_source_path"] = str(source)
    return rows


def _parse_each(
    tool_name: str,
    paths: Path | Iterable[Path],
    *,
    suffixes: tuple[str, ...] | None = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    selected = _as_paths(paths) if suffixes is None else _select_paths(paths, suffixes)
    rows: list[dict[str, Any]] = []
    for path in selected:
        rows.extend(_with_source(_run_extractor(tool_name, path, **kwargs), path))
    return rows


@contextmanager
def _fastqc_data(path: Path) -> Iterator[Path]:
    """Yield fastqc_data.txt directly or safely extract it from a FastQC ZIP."""

    if path.name == "fastqc_data.txt" or path.suffix.lower() == ".txt":
        yield path
        return
    if path.suffix.lower() != ".zip":
        raise ExtractorError(f"Unsupported FastQC input: {path}")

    try:
        with zipfile.ZipFile(path) as archive:
            members = [
                name for name in archive.namelist()
                if Path(name).name == "fastqc_data.txt" and not name.endswith("/")
            ]
            if len(members) != 1:
                raise ExtractorError(
                    f"{path} must contain exactly one fastqc_data.txt; found {len(members)}."
                )
            with tempfile.TemporaryDirectory(prefix="legacy-fastqc-") as temp_dir:
                target = Path(temp_dir) / "fastqc_data.txt"
                with archive.open(members[0]) as source, target.open("wb") as output:
                    output.write(source.read())
                yield target
    except zipfile.BadZipFile as exc:
        raise ExtractorError(f"Invalid FastQC ZIP: {path}") from exc


def _normalize(tool_name: str, row: dict[str, Any]) -> dict[str, Any]:
    if tool_name == "fastqc":
        if row.get("total_sequences") is not None:
            row.setdefault("total_reads", row["total_sequences"])
        if row.get("total_deduplicated_percentage") is not None:
            # bio-extractors 0.1.11 calculates 1 - deduplicated percentage,
            # so this value is the duplication rate despite its source name.
            row.setdefault(
                "duplication_rate",
                row["total_deduplicated_percentage"],
            )
    elif tool_name == "bbmap" and row.get("percent_mapped") is None:
        for alias in (
            "mapped_percent",
            "mapped_reads_percent",
            "mapping_percent",
            "mapped_percentage",
        ):
            if row.get(alias) is not None:
                row["percent_mapped"] = row[alias]
                break
    elif tool_name == "mlst" and row.get("species") is not None:
        row.setdefault("mlst_species", row["species"])
    elif tool_name == "bracken":
        aliases = {
            "D": "domain",
            "P": "phylum",
            "C": "class",
            "O": "order",
            "F": "family",
            "G": "genus",
            "G_taxonomy_id": "genus_taxonomy_id",
            "g_percent_sum": "genus_percent_sum",
        }
        for source, target in aliases.items():
            if row.get(source) is not None:
                row.setdefault(target, row[source])
    return row


def parse_fastqc(paths: Path | Iterable[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for archive_path in _as_paths(paths):
        with _fastqc_data(archive_path) as data_path:
            parsed = _run_extractor("fastqc", data_path)
        rows.extend(_with_source(parsed, archive_path))
    return [_normalize("fastqc", row) for row in rows]


def parse_mlst(paths: Path | Iterable[Path]) -> list[dict[str, Any]]:
    return [_normalize("mlst", row) for row in _parse_each("mlst", paths)]


def parse_quast(paths: Path | Iterable[Path]) -> list[dict[str, Any]]:
    return _parse_each("quast", paths, suffixes=(".tsv",))


def parse_bbmap(paths: Path | Iterable[Path]) -> list[dict[str, Any]]:
    candidates = _as_paths(paths)
    logs = [
        path for path in candidates
        if path.suffix.lower() == ".log" and "bbmap" in path.name.lower()
    ]
    selected = logs or [
        path for path in _as_paths(paths)
        if path.name.lower().endswith("_bb_stats.txt")
    ]
    return [_normalize("bbmap", row) for row in _parse_each("bbmap", selected)]


def parse_bracken(paths: Path | Iterable[Path]) -> list[dict[str, Any]]:
    candidates = _as_paths(paths)
    for ending in ("_kraken_report_bracken.txt", "_kraken_report.txt", "_bracken.out"):
        selected = [path for path in candidates if path.name.lower().endswith(ending)]
        if selected:
            return [
                _normalize("bracken", row)
                for row in _parse_each("bracken", selected)
            ]
    return []


def parse_amrfinder(paths: Path | Iterable[Path]) -> list[dict[str, Any]]:
    return _parse_each("amrfinder", paths, suffixes=("_amrfinder_report.tsv",))


def parse_prokka(paths: Path | Iterable[Path]) -> list[dict[str, Any]]:
    return _parse_each("prokka", paths, suffixes=(".gff", ".gff3"))


PARSERS = {
    "fastqc": parse_fastqc,
    "mlst": parse_mlst,
    "quast": parse_quast,
    "bbmap": parse_bbmap,
    "bracken": parse_bracken,
    "amrfinder": parse_amrfinder,
    "prokka": parse_prokka,
}

PRIMARY_KEYS = {
    "fastqc": "fastqc_id",
    "mlst": "mlst_id",
    "quast": "quast_id",
    "bbmap": "bbmap_id",
    "bracken": "bracken_id",
    "amrfinder": "amrfinder_id",
    "amrfinderplus": "amrfinderplus_id",
    "bakta_annotations": "bakta_annotation_id",
}


def _read_type_from_text(text: str) -> str | None:
    """Find R1/R2 from common read markers without matching sample numbers."""

    upper = text.upper()
    for marker, read_type in (("R1", "R1"), ("R2", "R2")):
        if re.search(rf"(^|[^A-Z0-9]){marker}([^A-Z0-9]|$)", upper):
            return read_type
    match = re.search(r"(^|[^A-Z0-9])([12])([^A-Z0-9]|$)", upper)
    if match:
        return f"R{match.group(2)}"
    return None


def _sequence_stem(filename: str) -> str:
    """Normalize FASTQ/FastQC filenames for read-to-report matching."""

    name = filename.lower()
    for suffix in (
        ".fastq.gz",
        ".fq.gz",
        ".fastq",
        ".fq",
        "_fastqc.txt",
        ".txt",
        "_fastqc.zip",
        ".zip",
    ):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _fastqc_read_type_from_source(
    row: dict[str, Any],
    path_hint: str,
    reads: dict[str, dict[str, Any]],
) -> str | None:
    explicit = str(row.get("read_type", "")).upper()
    if explicit in {"R1", "R2"}:
        return explicit

    text = " ".join(str(value) for value in row.values())
    detected = _read_type_from_text(f"{text} {path_hint}")
    if detected:
        return detected

    source_name = _sequence_stem(Path(path_hint).name)
    for read_type, read in reads.items():
        read_name = str(read.get("original_filename", ""))
        if source_name and source_name == _sequence_stem(read_name):
            return read_type
    return None


def _fastqc_read_id(
    row: dict[str, Any],
    path_hint: str,
    reads: dict[str, dict[str, Any]],
) -> Any:
    """Resolve an extractor row to R1 or R2 without assuming extractor IDs."""

    read_type = _fastqc_read_type_from_source(row, path_hint, reads)
    return reads.get(read_type or "", {}).get("read_id")


def _fastqc_read_type(
    row: dict[str, Any],
    path_hint: str,
    reads: dict[str, dict[str, Any]],
) -> str | None:
    return _fastqc_read_type_from_source(row, path_hint, reads)


def _require_fastqc_read_id(
    record: dict[str, Any],
    source_path: str,
    reads: dict[str, dict[str, Any]],
) -> None:
    """Fail before hitting the database when a FastQC row cannot map to a read."""

    if record.get("read_id") is not None:
        return
    available = {
        read_type: read.get("original_filename")
        for read_type, read in sorted(reads.items())
    }
    raise RuntimeError(
        "FastQC result could not be linked to an imported read before database "
        f"insert. source_path={source_path!r}, read_type={record.get('read_type')!r}, "
        f"available_reads={available!r}. Check that raw reads were imported and "
        "that the FastQC ZIP name or extractor fields identify R1/R2."
    )



def import_tool_results(
    connection,
    tool_name: str,
    paths: list[Path],
    sample_id: UUID,
    task_id: UUID,
    reads: dict[str, dict[str, Any]],
    result_table: str | None = None,
    timings: PhaseTimer | None = None,
) -> list[dict[str, Any]]:
    """Run one extractor, enrich every row, and idempotently upsert results."""

    try:
        parser = PARSERS[tool_name]
    except KeyError as exc:
        raise ExtractorError(f"No bio-extractors adapter is configured for {tool_name}.") from exc

    if timings:
        with timings.measure("extraction"):
            parsed = parser(paths)
    else:
        parsed = parser(paths)
    table = result_table or ("bakta_annotations" if tool_name == "prokka" else tool_name)
    primary_key = PRIMARY_KEYS.get(table, f"{table.rstrip('s')}_id")
    now = datetime.now(timezone.utc)
    enriched = []
    for index, values in enumerate(parsed):
        record = dict(values)
        source_path = str(record.pop("_source_path", ""))
        natural_key = (
            record.get("id")
            or record.get("locus_tag")
            or record.get("name")
            or record.get("gene")
            or index
        )
        extra_key = f"{source_path}:{natural_key}:{index}"
        record[primary_key] = result_id(table, sample_id, task_id, extra_key)
        record["sample_id"] = sample_id
        record["task_id"] = task_id
        record.setdefault("created_at", now)
        if table == "fastqc":
            record["read_id"] = _fastqc_read_id(record, source_path, reads)
            record["read_type"] = _fastqc_read_type(record, source_path, reads)
            _require_fastqc_read_id(record, source_path, reads)
        enriched.append(record)
    if timings:
        with timings.measure("database_write"):
            bulk_upsert(connection, table, enriched, [primary_key])
    else:
        bulk_upsert(connection, table, enriched, [primary_key])
    return enriched
