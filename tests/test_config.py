from pathlib import Path
from urllib.parse import unquote, urlsplit

from legacy_importer.config import load_config


def test_database_uri_prefers_complete_uri(monkeypatch):
    config = load_config("configs/legacy_import.yaml")
    expected = "postgresql://preferred:secret@db.example/test"
    monkeypatch.setenv("SEQQUERY_DATABASE_URI", expected)
    monkeypatch.setenv("AIRFLOW_CONN_POSTGRES", "postgresql://fallback:secret@db/fallback")
    assert config.database_uri() == expected


def test_database_uri_builds_from_legacy_components(monkeypatch):
    config = load_config("configs/legacy_import.yaml")
    monkeypatch.delenv("SEQQUERY_DATABASE_URI", raising=False)
    monkeypatch.delenv("AIRFLOW_CONN_POSTGRES", raising=False)
    monkeypatch.setenv("user", "legacy user")
    monkeypatch.setenv("pass", "p@ss/word")
    monkeypatch.setenv("db", "legacy-db")
    monkeypatch.delenv("SEQQUERY_DATABASE_USER", raising=False)
    monkeypatch.delenv("SEQQUERY_DATABASE_PASSWORD", raising=False)
    monkeypatch.delenv("SEQQUERY_DATABASE_NAME", raising=False)

    parsed = urlsplit(config.database_uri())
    assert unquote(parsed.username or "") == "legacy user"
    assert unquote(parsed.password or "") == "p@ss/word"
    assert parsed.hostname == "127.0.0.1"
    assert parsed.port == 5432
    assert parsed.path == "/legacy-db"


def test_configured_credentials_file_resolves_from_repository():
    config = load_config("configs/legacy_import.yaml")
    path = config.credentials_file()
    assert path is not None
    assert path.name == "legacy-importer.json"
    assert path.parent.name == ".secrets"
    assert path.is_absolute()


def test_configured_tools_match_supported_bio_extractors_adapters():
    from legacy_importer.import_results import PARSERS

    config = load_config("configs/legacy_import.yaml")
    extractor_tools = {
        name for name, tool in config.tools.items() if tool.extractor_enabled
    }
    assert extractor_tools == set(PARSERS)
    assert "kraken" not in config.tools


def test_legacy_samples_default_to_private_visibility():
    config = load_config("configs/legacy_import.yaml")
    assert config.samples.visibility == "private"


def test_storage_only_artifact_inputs_are_configured():
    config = load_config("configs/legacy_import.yaml")
    assert config.tools["prokka"].gcp_inputs == ["prokka/*"]
    assert config.tools["mash"].gcp_inputs == ["mash_k21_s1000/*.msh"]
    assert config.tools["ska"].gcp_inputs == ["ska_k15/*.skf"]
    assert not config.tools["mash"].extractor_enabled
    assert not config.tools["ska"].extractor_enabled
    assert not config.tools["export_summary"].extractor_enabled


def test_mash_ska_legacy_inputs_and_columns_are_configured():
    config = load_config("configs/legacy_import.yaml")
    assert config.mash_ska.enabled
    assert config.repo_path(config.mash_ska.mash_master_file).name == "all_sketches.msh"
    assert config.repo_path(config.mash_ska.ska_distances_csv).name == "ska_results.csv"
    assert config.mash_ska.ska_csv_columns.snps == "SNPs"
    assert config.mash_ska.ska_csv_columns.category is None
    assert not hasattr(config.mash_ska, "batch_id")
    assert not hasattr(config.mash_ska, "run_id")
