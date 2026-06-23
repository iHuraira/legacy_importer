from legacy_importer import ids


def test_ids_are_deterministic_and_entity_specific():
    first = ids.sample_id("mhh", "batch-a", "sample-1")
    assert first == ids.sample_id("mhh", "batch-a", "sample-1")
    assert first != ids.sample_id("mhh", "batch-a", "sample-2")
    assert first != ids.run_id("mhh", "batch-a")


def test_read_and_result_ids_include_disambiguating_keys():
    sample = ids.sample_id("mhh", "b", "s")
    task = ids.task_id(sample, "fastqc")
    assert ids.read_id(sample, "R1", "s_R1.fastq.gz") != ids.read_id(
        sample, "R2", "s_R2.fastq.gz"
    )
    assert ids.result_id("fastqc", sample, task, "R1") != ids.result_id(
        "fastqc", sample, task, "R2"
    )
