"""Bio-extractor adapters, row enrichment, and result insertion."""

from __future__ import annotations

import importlib
import tempfile
import zipfile
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from uuid import UUID

from .db import upsert
from .ids import result_id


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

    if path.name == "fastqc_data.txt":
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
    "bakta_annotations": "bakta_annotation_id",
}


def _fastqc_read_id(
    row: dict[str, Any],
    path_hint: str,
    reads: dict[str, dict[str, Any]],
) -> Any:
    """Resolve an extractor row to R1 or R2 without assuming extractor IDs."""

    text = (" ".join(str(value) for value in row.values()) + " " + path_hint).upper()
    if "_R1" in text or str(row.get("read_type", "")).upper() == "R1":
        return reads.get("R1", {}).get("read_id")
    if "_R2" in text or str(row.get("read_type", "")).upper() == "R2":
        return reads.get("R2", {}).get("read_id")
    return None


def _fastqc_read_type(row: dict[str, Any], path_hint: str) -> str | None:
    text = (" ".join(str(value) for value in row.values()) + " " + path_hint).upper()
    if "_R1" in text or str(row.get("read_type", "")).upper() == "R1":
        return "R1"
    if "_R2" in text or str(row.get("read_type", "")).upper() == "R2":
        return "R2"
    return None


def import_tool_results(
    connection,
    tool_name: str,
    paths: list[Path],
    sample_id: UUID,
    task_id: UUID,
    reads: dict[str, dict[str, Any]],
    result_table: str | None = None,
) -> list[dict[str, Any]]:
    """Run one extractor, enrich every row, and idempotently upsert results."""

    try:
        parser = PARSERS[tool_name]
    except KeyError as exc:
        raise ExtractorError(f"No bio-extractors adapter is configured for {tool_name}.") from exc

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
            record["read_type"] = _fastqc_read_type(record, source_path)
        upsert(connection, table, record, [primary_key])
        enriched.append(record)
    return enriched
