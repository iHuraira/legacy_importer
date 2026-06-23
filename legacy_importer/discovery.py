"""Discover raw reads and configured legacy tool outputs."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import ImportConfig


@dataclass(frozen=True)
class SampleInventory:
    sample_dir: Path
    reads: dict[str, Path] = field(default_factory=dict)
    tools: dict[str, list[Path]] = field(default_factory=dict)
    supplemental: dict[str, list[Path]] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        """Convert inventory paths to strings for JSON or CSV output."""

        return {
            "sample_dir": str(self.sample_dir),
            "exists": self.sample_dir.is_dir(),
            "r1": str(self.reads.get("R1", "")),
            "r2": str(self.reads.get("R2", "")),
            "tools": {name: [str(path) for path in paths] for name, paths in self.tools.items()},
            "supplemental": {
                name: [str(path) for path in paths] for name, paths in self.supplemental.items()
            },
            "missing": self.missing,
        }


def _glob_unique(root: Path, patterns: list[str]) -> list[Path]:
    """Expand patterns and return sorted unique files only."""

    paths = {path for pattern in patterns for path in root.glob(pattern) if path.is_file()}
    return sorted(paths, key=lambda path: path.as_posix())


def discover_gcp_inputs(
    sample_dir: str | Path,
    config: ImportConfig,
    tool_name: str,
) -> list[Path]:
    """Find the configured subset of tool files that should be archived to GCP."""

    root = Path(sample_dir)
    tool = config.tools[tool_name]
    return _glob_unique(root, tool.gcp_inputs)


def discover_sample(sample_dir: str | Path, config: ImportConfig) -> SampleInventory:
    """Find paired reads and all outputs enabled in the YAML tool definitions."""

    root = Path(sample_dir)
    if not root.is_dir():
        return SampleInventory(root, missing=["sample_directory"])

    reads: dict[str, Path] = {}
    for read_type in ("R1", "R2"):
        matches = sorted((root / "rawdata").glob(f"*_{read_type}.fastq.gz"))
        if len(matches) > 1:
            raise ValueError(f"Multiple {read_type} files found in {root / 'rawdata'}")
        if matches:
            reads[read_type] = matches[0]

    tools = {
        name: _glob_unique(root, tool.parser_inputs)
        for name, tool in config.tools.items()
        if tool.enabled
    }
    supplemental = {
        "quality_report": _glob_unique(root, ["*_quality_report.html"]),
        "summary": _glob_unique(root, ["*_summary.xlsx"]),
        "logs": _glob_unique(root, ["logs/*.log"]),
    }
    missing = [read_type.lower() for read_type in ("R1", "R2") if read_type not in reads]
    return SampleInventory(
        root, reads=reads, tools=tools, supplemental=supplemental, missing=missing
    )
