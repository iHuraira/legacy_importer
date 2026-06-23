"""Deterministic UUID5 identifiers used by every imported entity."""

from __future__ import annotations

from uuid import UUID, uuid5

NAMESPACE = UUID("719e7cad-f8b8-5ca6-8fae-b69c70aa4718")


def _id(kind: str, *parts: object) -> UUID:
    """Create a stable UUID from a record kind and normalized key parts."""

    value = "|".join([kind, *(str(part).strip() for part in parts)])
    return uuid5(NAMESPACE, value)


def organization_id(organization_code: str) -> UUID:
    return _id("organization", organization_code.lower())


def legacy_user_id(organization_code: str) -> UUID:
    return _id("legacy-user", organization_code.lower())


def batch_id(organization_code: str, legacy_batch_code: str) -> UUID:
    return _id("batch", organization_code.lower(), legacy_batch_code)


def job_id(organization_code: str, legacy_batch_code: str) -> UUID:
    return _id("job", organization_code.lower(), legacy_batch_code)


def run_id(organization_code: str, legacy_batch_code: str) -> UUID:
    return _id("run", organization_code.lower(), legacy_batch_code)


def sample_id(
    organization_code: str,
    legacy_batch_code: str,
    sample_name: str,
) -> UUID:
    return _id(
        "sample",
        organization_code.lower(),
        legacy_batch_code,
        sample_name,
    )


def read_id(sample_id_value: UUID, read_type: str, original_filename: str) -> UUID:
    return _id("read", sample_id_value, read_type.upper(), original_filename)


def task_id(sample_id_value: UUID, tool_name: str) -> UUID:
    return _id("task", sample_id_value, tool_name.lower())


def artifact_id(sample_id_value: UUID, tool_name: str, artifact_kind: str = "output") -> UUID:
    return _id("artifact", sample_id_value, tool_name.lower(), artifact_kind)


def result_id(table_name: str, sample_id_value: UUID, task_id_value: UUID, extra_key: object = "") -> UUID:
    return _id("result", table_name.lower(), sample_id_value, task_id_value, extra_key)


def qc_id(gate_name: str, sample_id_value: UUID, gate_version: str) -> UUID:
    return _id("qc", gate_name.lower(), sample_id_value, gate_version)


def global_batch_id() -> UUID:
    return _id("batch", "legacy", "global")


def global_organization_id() -> UUID:
    return _id("organization", "legacy-global")


def global_user_id() -> UUID:
    return _id("legacy-user", "legacy-global")


def global_job_id(batch_id_value: UUID) -> UUID:
    return _id("job", batch_id_value, "legacy-global")


def global_run_id(batch_id_value: UUID) -> UUID:
    return _id("run", batch_id_value, "legacy-global")


def batch_task_id(batch_id_value: UUID, tool_name: str, version: object) -> UUID:
    return _id("batch-task", batch_id_value, tool_name.lower(), version)


def batch_artifact_id(task_id_value: UUID, artifact_name: str) -> UUID:
    return _id("batch-artifact", task_id_value, artifact_name.lower())


def mash_master_id(batch_id_value: UUID, version: object) -> UUID:
    return _id("mash-master", batch_id_value, version)


def ska_master_id(batch_id_value: UUID, version: object) -> UUID:
    return _id("ska-master", batch_id_value, version)


def ska_distance_id(
    ska_master_id_value: UUID,
    sample1_id: UUID,
    sample2_id: UUID,
) -> UUID:
    first, second = sorted((str(sample1_id), str(sample2_id)))
    return _id("ska-distance", ska_master_id_value, first, second)
