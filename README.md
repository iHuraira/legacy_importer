# Legacy Importer

Standalone, idempotent importer for historical DNA sequencing reads and existing
pipeline outputs. It does not run bioinformatics tools. Imported identifiers are
deterministic UUID5 values, so rerunning the same manifest is safe.

## Install

```bash
python -m venv .venv
.venv/bin/pip install .
```

On Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\python -m pip install .
```

Use an editable install only for development:

```bash
pip install -e ".[test]"
```

Run the importer from the staged repository directory, or keep absolute paths
to the configuration, manifest, MASH master, and SKA CSV. The large legacy
input files and deployment configuration are intentionally not embedded in the
Python wheel.

## Configure credentials

Preferred PostgreSQL configuration:

```bash
export SEQQUERY_DATABASE_URI="$AIRFLOW_CONN_POSTGRES"
```

The importer also supports component variables:

```bash
export SEQQUERY_DATABASE_USER="postgres"
export SEQQUERY_DATABASE_PASSWORD="secret"
export SEQQUERY_DATABASE_NAME="seqquery"
export SEQQUERY_DATABASE_HOST="127.0.0.1"
export SEQQUERY_DATABASE_PORT="5432"
```

For backward compatibility, local `.env` entries named `user`, `pass`, and
`db` are accepted, including the legacy `name: value` format. Host and port
default to `127.0.0.1:5432`.

Set GCP using a file override:

```bash
export GOOGLE_APPLICATION_CREDENTIALS="/secure/path/service-account.json"
```

or in-memory JSON:

```bash
export SEQQUERY_GCP_CREDENTIALS="$(cat /secure/path/service-account.json)"
```

When neither override is set, the provided configuration resolves
`.secrets/legacy-importer.json` relative to the repository. Both `.env` and
`.secrets/` are ignored by Git. Never commit credential values.

## Validate connections and schema

```bash
legacy-import test-connections --config configs/legacy_import.yaml
legacy-import inspect-schema --config configs/legacy_import.yaml
```

`test-db --write-test` creates a temporary table and rolls the transaction back.
`test-gcp` uploads, verifies, and deletes a tiny healthcheck object.

Before the production run:

1. Confirm every `full_path` is mounted on the machine running the importer.
2. Resolve the documented `SH176x65` SKA sample override.
3. Run `test-connections`, `inspect-schema`, and an `inventory` report.

## Inventory and dry run

```bash
legacy-import inventory --csv samples.csv --config configs/legacy_import.yaml --out inventory.csv
legacy-import dry-run --csv samples.csv --config configs/legacy_import.yaml --limit 5
```

Inventory never contacts PostgreSQL or GCS. Dry run validates the database
schema, computes identifiers, and reports planned writes and uploads without
performing them.

The finalized CSV manifest has six columns:

```csv
sample_name,full_path,organization_code,organization_name,legacy_batch_code,visibility
```

`visibility` must be either `public` or `private`; when omitted, it defaults to
`private`. `organization_name` is optional for older manifests and falls back
to the configured organization mapping. `level_2` is intentionally ignored.

## Import

One sample:

```bash
legacy-import import --csv samples.csv --config configs/legacy_import.yaml --sample-name F1S1R3D2B2P3G09
```

To populate only the full-fidelity `amrfinderplus` result table from existing
legacy reports and link each row to its existing `amrfinder` task:

```powershell
legacy-import import-amrfinderplus `
  --csv data_info.csv `
  --config configs/legacy_import.yaml `
  --dry-run

legacy-import import-amrfinderplus `
  --csv data_info.csv `
  --config configs/legacy_import.yaml
```

The command does not create tasks, samples, reads, artifacts, QC rows, or other
tool results. It requires exactly one existing `amrfinder` task and one
`*_amrfinder_report.tsv` for each selected sample. Re-running it safely upserts
the same deterministic `amrfinderplus_id` values.

Sample imports show an overall upload progress bar. Resume mode is enabled by
default: committed samples are skipped and failures are reported while later
samples continue. Use `--no-resume` to stop after the first failure, and
`--no-progress` for automation or non-interactive logs.

The importer opens a fresh PostgreSQL connection for every sample instead of
holding one session for the entire CSV. If PostgreSQL drops a connection, the
current sample is retried up to three times by default with exponential
backoff. Configure this with:

```bash
legacy-import import \
  --csv data_info.csv \
  --config configs/legacy_import.yaml \
  --database-retries 5 \
  --retry-base-seconds 2
```

Only connection-level failures are retried automatically. Data validation,
missing-file, extractor, and schema errors are recorded immediately and the
import proceeds to the next sample while resume mode is enabled.

At startup, resume mode loads the selected existing sample IDs in database
batches and keeps them in memory. Already-imported samples are therefore skipped
without opening a new PostgreSQL connection for each one. Fresh per-sample
connections are created only for samples that need work.

After each sample, the importer prints the sample name and total elapsed import
time. This includes local hashing/archive work, GCS uploads, extraction, QC,
and database writes.

It also prints accumulated non-overlapping phase timings:

```text
S1: imported in 295.7s (database attempts: 1)
  timings:
    archive_creation: 83.4s
    checksum: 22.6s
    database_connection: 0.2s
    database_write: 4.7s
    extraction: 15.1s
    gcs_upload: 161.8s
    input_discovery: 2.1s
    qc/verification: 2.4s
    untracked_overhead: 3.4s
```

Repeated phases across reads and tools are accumulated. The same timing map is
included in the final JSON report and written as a compact structured logging
entry.

Extractor result rows are bulk-upserted in pages of 1,000, and database table
column metadata is cached for the lifetime of each sample connection. A Prokka
result with roughly 4,400 annotations therefore uses about five bulk inserts
instead of thousands of individual inserts and schema queries.

Artifact archives use gzip compression level 1 by default:

```yaml
artifacts:
  output_type: tar.gz
  compression_level: 1
```

This keeps standard `.tar.gz` compatibility and deterministic output while
favoring speed over the smaller files produced by gzip level 9. Levels from
`0` to `9` are accepted. Level `1` is recommended for the migration because
network upload is already faster than archive compression.

If PostgreSQL reports a database collation version mismatch, the importer logs
one administrative warning. Connection startup temporarily suppresses server
warnings, the importer checks the stored and actual collation versions with a
read-only query, and normal PostgreSQL warnings are immediately restored.
Database errors remain visible. The importer never runs `ALTER DATABASE`;
collation refresh remains a database administrator task.

## Legacy global MASH and SKA

The importer registers the existing merged MASH master and imports the
precomputed SKA CSV. It does not execute MASH or SKA.

The importer automatically creates or reuses a dedicated synthetic ownership
chain. Its batch uses accession `LEGACY-GLOBAL-BATCH` and name
`Legacy Global Import`; its run uses accession `LEGACY-GLOBAL-RUN`. Both use
deterministic UUID5 IDs and `source: legacy_import`. No batch or run IDs need
to be configured manually.

```bash
legacy-import import-mash-master --config configs/legacy_import.yaml
legacy-import import-ska-distances --config configs/legacy_import.yaml
legacy-import import-mash-ska --config configs/legacy_import.yaml
legacy-import verify-mash-ska --config configs/legacy_import.yaml
```

These commands show progress bars by default. Use `--no-progress` for logs,
automation, or non-interactive execution. SKA checkpoint resume is enabled by
default. Use `--no-resume` only when intentionally restarting SKA from CSV row
one; deterministic IDs still prevent duplicate database records.

The MASH archive is uploaded using the existing batch artifact convention:

```text
gs://{artifact_bucket}/{run_id}/batch/{task_id}.tar.gz
```

The supplied manifest contains two records named `SH176x65`, while the SKA CSV
does not contain a batch identifier. Configure the intended record before the
SKA import:

```yaml
mash_ska:
  ska_sample_overrides:
    SH176x65:
      organization_code: copenhagen
      legacy_batch_code: REPLACE_WITH_THE_CORRECT_BATCH
      sample_name: SH176x65
```

The importer fails with an ambiguous-sample report until this is resolved. It
does not guess or alter the historical CSV.

All samples, explicitly using the default resume behavior:

```bash
legacy-import import --csv samples.csv --config configs/legacy_import.yaml --resume
```

Available controls include `--limit`, `--organization`, `--skip-existing`,
`--skip-reads`, `--skip-artifacts`, `--skip-extractors`, and `--skip-qc`.
Each sample uses an explicit database transaction. GCS uploads happen before
their database rows; retries reuse deterministic object names.

If the process stops:

- Fully committed samples remain complete and are skipped on restart.
- The interrupted sample has its database transaction rolled back. Restarting
  uploads it again to the same deterministic GCS object names and completes it.
- A dropped PostgreSQL session is closed safely, a new connection is opened,
  and the interrupted sample is retried. Rollback cleanup never replaces the
  original error when the connection is already closed.
- MASH registration is idempotent and safely reuses deterministic IDs.
- SKA commits in batches and stores its last committed CSV row under
  `.legacy-import-state/`. Restarting continues from that row. The checkpoint
  is removed after successful completion.

Sample failures are appended immediately to:

```text
.legacy-import-state/sample_failures.jsonl
```

Each line includes the timestamp, sample, organization, batch, elapsed time,
and sanitized error. Override the location with `--failure-report PATH`.
Failures do not stop later samples while resume mode is enabled.

## Database migration 13

New imports populate the extended pipeline ownership fields:

- `organizations.auth_id` and `organizations.auth_name` are populated when
  configured under the matching organization in `configs/legacy_import.yaml`.
  They are omitted when not configured, so existing Keycloak values are not
  overwritten with null values.
- `batches.finished_at` is set for imported historical batches.
- Sample `tasks.organization_id` and `artifacts.organization_id` use the
  sample's database organization.
- Global MASH/SKA tasks and the global MASH artifact use
  `organization_id = NULL`, consistent with their global scope.

## Source data safety

Files and directories referenced by the manifest `full_path` column are
read-only inputs. The importer opens them for reading, hashing, parsing, and
uploading only. It never moves, renames, truncates, or deletes them.

Archives and generated summaries are created outside the source directory and
only those generated temporary files are deleted. Archive creation explicitly
rejects an output location inside a source sample directory. Inventory output
is also rejected when its destination is inside a manifest source directory.

`test-gcp` deletes only the healthcheck object that it created in GCS. Import
commands never delete source files or source GCS objects.

Raw R1/R2 files are uploaded directly with their original `.fastq.gz`
filenames. All Prokka outputs, Mash `.msh`, and SKA `.skf` files are wrapped in
tool-specific `.tar.gz` artifacts and uploaded when present. All configured
analysis outputs are still parsed into database result tables even when their
source files are not stored in GCP.

After extraction and QC, the importer builds a one-row summary CSV directly
from the normalized in-memory results. It uploads that CSV under a synthetic
`export_summary` task. No database summary view or query is required.

## Extractor integration

Extractor imports are deliberately lazy and isolated in
`legacy_importer/import_results.py`. The adapters call the public
`bio_extractors.run_extractor(tool, path)` API from `bio-extractors==0.1.11`.
The importer selects the parser-compatible file from each artifact group and
extracts `fastqc_data.txt` from FastQC ZIPs. FastQC fractions are preserved for
the paired-read QC1 calculation. Extractors return parsed values; this package
adds primary keys, sample/task/read IDs, and timestamps.

QC1 requires both R1 and R2 FastQC rows. It calculates small- and large-genome
coverage, worst-pair duplication and N content, average GC content, read-count
imbalance, and validates the supported read lengths 125, 150, 250, and 300.
The persisted gate version is `qc_gate_loose_v1`.

QC2 requires BBMap and QUAST results. It evaluates mean coverage, mapped-read
percentage, reference breadth, assembly length, contig count, largest contig,
N50, and assembly GC percentage. Its persisted gate version is
`qc2_rules_v1`.

Kraken is not configured because bio-extractors does not provide a Kraken
integration. Bracken consumes the available hierarchical Kraken/Bracken report.
Prokka parses only GFF/GFF3 files and AMRFinder parses only report TSV files.
For BBMap, the adapter prefers `logs/bbmap_*.log`, which contains the summary
metrics required by QC2. These files are parsed but are not uploaded. The
Prokka GFF is parsed for annotations, while the complete `prokka/` output
directory is archived to GCP.
Optional `mash_k21_s1000/*.msh` and `ska_k15/*.skf` sketches are storage-only
artifacts and are not sent to the Mash/SKA distance parsers.

## Verify

```bash
legacy-import verify --csv samples.csv --config configs/legacy_import.yaml
```

Verification checks ownership, sample, reads, synthetic tasks, artifacts,
result/QC rows, and the existence of referenced GCS objects.

## Tests

```bash
pytest
```

The importer filters row fields against the deployed table columns before
upserting. This supports minor schema differences, while `inspect-schema`
reports missing required core columns and result tables explicitly.
