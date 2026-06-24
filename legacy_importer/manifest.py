"""CSV manifest loading, validation, and filtering."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, field_validator

REQUIRED_COLUMNS = {
    "sample_name",
    "full_path",
    "organization_code",
    "legacy_batch_code",
}


class ManifestRow(BaseModel):
    sample_name: str
    full_path: Path
    organization_code: str
    organization_name: str | None = None
    legacy_batch_code: str
    visibility: Literal["public", "private"] = "private"

    @field_validator("sample_name", "organization_code", "legacy_batch_code")
    @classmethod
    def non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value cannot be empty")
        return value

    @field_validator("organization_name")
    @classmethod
    def normalize_optional_name(cls, value: str | None) -> str | None:
        """Normalize an optional organization display name."""

        if value is None:
            return None
        return value.strip() or None

    def source_metadata(self) -> dict[str, str]:
        """Return JSON-safe metadata preserving the original manifest fields."""

        metadata = {
            "sample_name": self.sample_name,
            "full_path": str(self.full_path),
            "organization_code": self.organization_code,
            "legacy_batch_code": self.legacy_batch_code,
            "visibility": self.visibility,
        }
        if self.organization_name:
            metadata["organization_name"] = self.organization_name
        return metadata


def read_manifest(
    path: str | Path,
    organization: str | None = None,
    sample_name: str | None = None,
    limit: int | None = None,
) -> list[ManifestRow]:
    """Read validated manifest rows and apply optional CLI filters."""

    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Manifest is missing columns: {', '.join(sorted(missing))}")
        rows = [ManifestRow.model_validate(row) for row in reader]
    if organization:
        rows = [row for row in rows if row.organization_code == organization]
    if sample_name:
        rows = [row for row in rows if row.sample_name == sample_name]
    return rows[:limit] if limit is not None else rows
