"""Google Cloud Storage authentication, upload, and health checks."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

from google.cloud import storage
from google.oauth2 import service_account

from .config import ImportConfig


def client_from_config(config: ImportConfig) -> storage.Client:
    """Create a GCS client from JSON credentials or application defaults."""

    raw_json = os.getenv(config.gcp.credentials_json_env)
    if raw_json:
        credentials = service_account.Credentials.from_service_account_info(json.loads(raw_json))
        return storage.Client(credentials=credentials, project=credentials.project_id)
    credentials_file = os.getenv(config.gcp.credentials_file_env)
    if credentials_file:
        credentials = service_account.Credentials.from_service_account_file(credentials_file)
        return storage.Client(credentials=credentials, project=credentials.project_id)
    configured_file = config.credentials_file()
    if configured_file:
        if not configured_file.is_file():
            raise RuntimeError(
                f"Configured GCP credentials file does not exist: {configured_file}"
            )
        credentials = service_account.Credentials.from_service_account_file(configured_file)
        return storage.Client(credentials=credentials, project=credentials.project_id)
    return storage.Client()


def upload_file(client: storage.Client, bucket_name: str, object_name: str, path: Path) -> dict[str, object]:
    """Upload a local file and return metadata confirmed by GCS."""

    blob = client.bucket(bucket_name).blob(object_name)
    blob.upload_from_filename(str(path), checksum="auto")
    blob.reload()
    return {
        "uri": f"gs://{bucket_name}/{object_name}",
        "size_bytes": int(blob.size or path.stat().st_size),
        "checksum": blob.md5_hash or blob.crc32c,
    }


def object_exists(client: storage.Client, uri: str) -> bool:
    """Check whether a gs:// object exists."""

    bucket, object_name = uri[5:].split("/", 1)
    return client.bucket(bucket).blob(object_name).exists()


def test_gcp(config: ImportConfig) -> dict[str, object]:
    """Upload, verify, and delete a tiny object in each configured bucket."""

    client = client_from_config(config)
    results = {}
    for bucket_name in sorted({config.gcp.artifact_bucket, config.gcp.raw_reads_bucket}):
        object_name = f"legacy_import/healthchecks/{uuid4()}.txt"
        blob = client.bucket(bucket_name).blob(object_name)
        try:
            blob.upload_from_string(b"legacy-import healthcheck")
            exists = blob.exists()
        finally:
            blob.delete(if_generation_match=blob.generation) if blob.exists() else None
        results[bucket_name] = {"upload": True, "exists": exists, "deleted": not blob.exists()}
    return results
