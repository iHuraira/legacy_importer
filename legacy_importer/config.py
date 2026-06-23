"""Typed YAML configuration and environment-based credential settings."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import yaml
from pydantic import BaseModel, Field


class DatabaseConfig(BaseModel):
    env_primary: str = "SEQQUERY_DATABASE_URI"
    env_fallback: str = "AIRFLOW_CONN_POSTGRES"
    component_env: dict[str, str] = Field(
        default_factory=lambda: {
            "user": "SEQQUERY_DATABASE_USER",
            "password": "SEQQUERY_DATABASE_PASSWORD",
            "database": "SEQQUERY_DATABASE_NAME",
            "host": "SEQQUERY_DATABASE_HOST",
            "port": "SEQQUERY_DATABASE_PORT",
            "sslmode": "SEQQUERY_DATABASE_SSLMODE",
        }
    )
    legacy_component_env: dict[str, str] = Field(
        default_factory=lambda: {"user": "user", "password": "pass", "database": "db"}
    )
    default_host: str = "127.0.0.1"
    default_port: int = 5432
    default_sslmode: str = "prefer"


class GCPConfig(BaseModel):
    artifact_bucket: str
    raw_reads_bucket: str
    credentials_file_env: str = "GOOGLE_APPLICATION_CREDENTIALS"
    credentials_json_env: str = "SEQQUERY_GCP_CREDENTIALS"
    credentials_file: Path | None = None
    raw_reads_prefix: str = "legacy_import/raw_reads"


class ToolConfig(BaseModel):
    enabled: bool = True
    category: str
    parser_inputs: list[str]
    gcp_inputs: list[str] = Field(default_factory=list)
    extractor_enabled: bool = True
    result_table: str | None = None


class SamplesConfig(BaseModel):
    status: str = "imported"
    accession_from: str = "sample_name"
    visibility: Literal["public", "private"] = "private"


class QCGateConfig(BaseModel):
    enabled: bool = True
    gate_version: str
    thresholds: dict[str, float | int] = Field(default_factory=dict)


class QCConfig(BaseModel):
    qc1: QCGateConfig
    qc2: QCGateConfig


class MashSkaSampleSourceConfig(BaseModel):
    mode: Literal["manifest", "database", "manifest_or_database"] = "manifest_or_database"
    manifest_csv: Path | None = None
    source_filter: str | None = "legacy_import"


class SkaCsvColumnsConfig(BaseModel):
    sample1: str = "sample1"
    sample2: str = "sample2"
    snps: str = "snps"
    category: str | None = "category"


class SkaCategoryThresholdsConfig(BaseModel):
    close_max_snps: int = 20
    related_max_snps: int = 100
    default_category: str = "unrelated"


class SkaSampleOverrideConfig(BaseModel):
    organization_code: str
    legacy_batch_code: str
    sample_name: str


class MashSkaConfig(BaseModel):
    enabled: bool = False
    mash_master_file: Path = Path("legacy_master.msh")
    mash_version: int | str = 1
    mash_is_active: bool = True
    mash_scope: Literal["global"] = "global"
    batch_sketch_count: int | None = None
    cumulative_sketch_count: int | None = None
    ska_distances_csv: Path = Path("legacy_ska_distances.csv")
    ska_version: int | str = 1
    checkpoint_dir: Path = Path(".legacy-import-state")
    sample_source: MashSkaSampleSourceConfig = Field(
        default_factory=MashSkaSampleSourceConfig
    )
    ska_csv_columns: SkaCsvColumnsConfig = Field(default_factory=SkaCsvColumnsConfig)
    ska_sample_overrides: dict[str, SkaSampleOverrideConfig] = Field(
        default_factory=dict
    )
    ska_category_thresholds: SkaCategoryThresholdsConfig = Field(
        default_factory=SkaCategoryThresholdsConfig
    )


class ImportConfig(BaseModel):
    source: str = "legacy_import"
    database: DatabaseConfig
    gcp: GCPConfig
    organizations: dict[str, dict[str, str]] = Field(default_factory=dict)
    legacy_ownership: dict[str, Any]
    samples: SamplesConfig
    reads: dict[str, Any]
    artifacts: dict[str, Any]
    tasks: dict[str, Any]
    tools: dict[str, ToolConfig]
    qc: QCConfig
    mash_ska: MashSkaConfig = Field(default_factory=MashSkaConfig)
    config_dir: Path = Field(default=Path("."), exclude=True)

    def database_uri(self) -> str:
        """Get the database URI without logging or otherwise exposing it."""

        uri = os.getenv(self.database.env_primary) or os.getenv(self.database.env_fallback)
        if uri:
            return uri

        def component(name: str) -> str | None:
            preferred = self.database.component_env.get(name)
            legacy = self.database.legacy_component_env.get(name)
            return (os.getenv(preferred) if preferred else None) or (
                os.getenv(legacy) if legacy else None
            )

        user = component("user")
        password = component("password")
        database = component("database")
        if not all((user, password, database)):
            raise RuntimeError(
                f"Set {self.database.env_primary}, {self.database.env_fallback}, "
                "or the configured database user/password/name component variables."
            )
        host = component("host") or self.database.default_host
        port = component("port") or str(self.database.default_port)
        sslmode = component("sslmode") or self.database.default_sslmode
        return (
            f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}"
            f"@{host}:{port}/{quote(database, safe='')}?sslmode={quote(sslmode, safe='')}"
        )

    def organization_name(self, code: str) -> str:
        return self.organizations.get(code, {}).get("organization_name", code)

    def credentials_file(self) -> Path | None:
        """Resolve an optional configured credential path relative to the repo root."""

        path = self.gcp.credentials_file
        if path is None:
            return None
        if path.is_absolute():
            return path
        return (self.config_dir.parent / path).resolve()

    def repo_path(self, path: Path) -> Path:
        """Resolve a configured input path relative to the repository root."""

        if path.is_absolute():
            return path
        return (self.config_dir.parent / path).resolve()


def _load_local_env(path: Path) -> None:
    """Load local KEY=value or legacy name: value entries without logging values."""

    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" in line:
            name, value = line.split("=", 1)
        elif ":" in line:
            name, value = line.split(":", 1)
        else:
            continue
        name, value = name.strip(), value.strip().strip("'\"")
        if name and value:
            os.environ.setdefault(name, value)


def load_config(path: str | Path) -> ImportConfig:
    """Load environment variables and validate the importer YAML file."""

    config_path = Path(path).resolve()
    repo_root = config_path.parent.parent
    env_path = repo_root / ".env"
    _load_local_env(env_path)
    with config_path.open("r", encoding="utf-8") as handle:
        config = ImportConfig.model_validate(yaml.safe_load(handle))
    config.config_dir = config_path.parent
    return config
