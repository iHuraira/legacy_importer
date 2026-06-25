"""Create deterministic tar.gz artifacts with an output/ root directory."""

from __future__ import annotations

import gzip
import hashlib
import os
import tarfile
from pathlib import Path


def sha256_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    """Calculate a streaming SHA-256 checksum for a local file."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_archive(
    sample_dir: str | Path,
    inputs: list[Path],
    artifact_id: object,
    temp_dir: str | Path | None = None,
    compression_level: int = 1,
) -> Path:
    """Archive selected files under output/ while preserving relative paths."""

    root = Path(sample_dir).resolve()
    destination = Path(temp_dir or os.getenv("TMPDIR") or "/tmp")
    destination.mkdir(parents=True, exist_ok=True)
    output = (destination / f"{artifact_id}.tar.gz").resolve()
    if output.is_relative_to(root):
        raise ValueError(
            f"Archive output must be outside source data directory {root}: {output}"
        )

    resolved = {path.resolve() for path in inputs}
    for path in resolved:
        if not path.is_file() or not path.is_relative_to(root):
            raise ValueError(f"Archive input must be a file below {root}: {path}")
    resolved = sorted(
        resolved,
        key=lambda path: (
            len(path.relative_to(root).parts),
            path.relative_to(root).as_posix(),
        ),
    )

    if not 0 <= compression_level <= 9:
        raise ValueError("compression_level must be between 0 and 9")

    # Level 1 favors import speed while retaining standard tar.gz compatibility.
    # Stable metadata keeps archives reproducible for a fixed compression level.
    with output.open("wb") as raw:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            fileobj=raw,
            compresslevel=compression_level,
            mtime=0,
        ) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                for path in resolved:
                    arcname = Path("output") / path.relative_to(root)
                    info = archive.gettarinfo(str(path), arcname=arcname.as_posix())
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    info.mtime = 0
                    with path.open("rb") as handle:
                        archive.addfile(info, handle)
    return output
