# Legacy Importer: Detailed Repository Report

Report updated: 2026-06-19

## 1. Executive summary

`legacy_importer` is a standalone Python package for migrating historical DNA
sequencing data into an existing PostgreSQL database and Google Cloud Storage
(GCS).

The package does **not** execute sequencing or bioinformatics pipelines. It
works with pipeline outputs that already exist on disk. Its responsibilities
are:

1. Read and validate a CSV sample manifest.
2. Discover raw FASTQ files and existing analysis outputs.
3. Build stable ownership records for organizations, users, batches, jobs,
   runs, and samples.
4. Upload raw reads to GCS and register them in PostgreSQL.
5. Package tool outputs into reproducible `tar.gz` archives.
6. Upload artifact archives and register synthetic tool tasks and artifacts.
7. Call `bio-extractors==0.1.11` through
   `bio_extractors.run_extractor(tool, path)`.
8. Add importer-owned UUIDs, foreign keys, and timestamps to parsed results.
9. Insert or update result-table records.
10. Calculate paired-read QC1 and BBMap/QUAST QC2 using the integrated
    production rule sets.
11. Verify database records and GCS objects after import.

The main safety mechanism is deterministic UUID5 generation. Given the same
organization, batch, sample, tool, and filename, the importer generates the
same identifiers every time. Database writes use PostgreSQL upserts, making
normal reruns idempotent.

---

## 2. Repository structure

```text
.
├── pyproject.toml
├── README.md
├── REPOSITORY_REPORT.md
├── .env.example
├── configs/
│   └── legacy_import.yaml
├── legacy_importer/
│   ├── __init__.py
│   ├── cli.py
│   ├── config.py
│   ├── ids.py
│   ├── manifest.py
│   ├── schema.py
│   ├── db.py
│   ├── gcs.py
│   ├── discovery.py
│   ├── archive.py
│   ├── import_ownership.py
│   ├── import_reads.py
│   ├── import_artifacts.py
│   ├── import_results.py
│   ├── qc.py
│   ├── import_sample.py
│   ├── verify.py
│   └── logging_config.py
└── tests/
    ├── test_ids.py
    ├── test_manifest.py
    ├── test_discovery.py
    ├── test_archive.py
    ├── test_qc.py
    └── test_schema.py
```

---

## 3. High-level architecture

The package is split into five layers:

| Layer | Files | Responsibility |
|---|---|---|
| User interface | `cli.py` | Defines all `legacy-import` commands and options. |
| Input and configuration | `config.py`, `manifest.py`, `discovery.py` | Loads YAML and CSV data and finds local files. |
| Infrastructure | `db.py`, `gcs.py`, `schema.py`, `archive.py` | Handles PostgreSQL, GCS, schema validation, checksums, and archives. |
| Import domain logic | `ids.py`, `import_ownership.py`, `import_reads.py`, `import_artifacts.py`, `import_results.py`, `qc.py` | Builds identifiers and imports each category of data. |
| Orchestration and verification | `import_sample.py`, `verify.py` | Coordinates one sample transaction and validates imported state. |

### Main import flow

```text
CLI import command
    |
    v
Load YAML configuration
    |
    v
Read and filter CSV manifest
    |
    v
For each sample
    |
    +--> Discover FASTQ and result files
    |
    +--> Build deterministic ownership IDs and rows
    |
    +--> Start PostgreSQL transaction
            |
            +--> Upsert organization/user/batch/job/run/sample
            |
            +--> Upload and upsert raw reads
            |
            +--> For each discovered tool
            |       |
            |       +--> Upsert synthetic task
            |       +--> Create deterministic tar.gz archive
            |       +--> Upload archive to GCS
            |       +--> Upsert artifact after upload succeeds
            |       +--> Run extractor
            |       +--> Enrich and upsert result rows
            |
            +--> Calculate and upsert QC1 and QC2
            |
            +--> Commit transaction
```

If an exception occurs during the database portion, the sample transaction is
rolled back. Uploaded GCS objects may remain, but they use deterministic object
names and can be safely reused or overwritten during a retry.

---

## 4. Data model and deterministic identifiers

All imported data identifiers are generated in `ids.py` using UUID5 and a
fixed namespace:

```text
719e7cad-f8b8-5ca6-8fae-b69c70aa4718
```

UUID5 is deterministic: the same namespace and key string always produce the
same UUID.

| Entity | Stable key inputs |
|---|---|
| Organization | organization code |
| Legacy user | organization code |
| Batch | organization code + legacy batch code |
| Job | organization code + legacy batch code |
| Run | organization code + legacy batch code |
| Sample | organization code + legacy batch code + sample name |
| Read | sample ID + read type + original filename |
| Task | sample ID + tool name |
| Artifact | sample ID + tool name + artifact kind |
| Result | table name + sample ID + task ID + row-specific key |
| QC record | gate name + sample ID + gate version |

The identifiers make reruns safe even if processing order changes.

Organizations receive special handling. Before inserting an organization, the
importer looks it up by `organization_code`. If an organization already exists
with a different UUID, its existing database UUID is reused in dependent
organization, user, batch, and sample relationships.

Samples include a required `visibility` value of `public` or `private`.
Historical imports default to `private`. Private samples remain queryable for
dashboard display; download authorization for their reads and artifacts is
enforced by the consuming application.

---

## 5. Configuration behavior

The default configuration is stored in `configs/legacy_import.yaml`.

### Database configuration

The importer first reads:

```text
SEQQUERY_DATABASE_URI
```

If it is absent, it falls back to:

```text
AIRFLOW_CONN_POSTGRES
```

The URI is never deliberately printed. Logging includes a filter that redacts
passwords embedded in URL-like strings.

### GCS configuration

The configuration defines:

- Artifact bucket
- Raw-read bucket
- Raw-read object prefix
- Credentials file environment-variable name
- Credentials JSON environment-variable name

Credential resolution works as follows:

1. If `SEQQUERY_GCP_CREDENTIALS` contains service-account JSON, create explicit
   credentials from that JSON.
2. If `GOOGLE_APPLICATION_CREDENTIALS` points to a file, load that file.
3. If neither override is set, load the configured repository-relative
   `.secrets/legacy-importer.json` file.
4. If no credential source is configured, use Google Application Default
   Credentials.

The database connection uses this precedence:

1. `SEQQUERY_DATABASE_URI`
2. `AIRFLOW_CONN_POSTGRES`
3. `SEQQUERY_DATABASE_USER`, `SEQQUERY_DATABASE_PASSWORD`, and
   `SEQQUERY_DATABASE_NAME`, plus optional host, port, and SSL mode variables
4. Legacy local names `user`, `pass`, and `db`

Legacy `.env` lines using `name: value` are supported. Host and port default to
`127.0.0.1:5432` when component variables are used.

### Tool configuration

Each tool has:

- `enabled`: whether discovery/import is active.
- `category`: artifact classification stored in the database.
- `parser_inputs`: files discovered for extractor/database processing.
- `gcp_inputs`: optional subset archived and uploaded to GCP.
- Optional `result_table`: overrides the result-table name.

The configured tools are:

- FastQC
- BBMap
- Bracken
- MLST
- Quast
- AMRFinder
- Prokka

Prokka results default to the `bakta_annotations` result table because the
target database may use a shared annotation schema.

Kraken is not configured as a standalone extractor. Its hierarchical reports
are consumed by the Bracken adapter.

### QC configuration

QC1 and QC2 have independent enable flags and gate versions:

- QC1: `qc_gate_loose_v1`
- QC2: `qc2_rules_v1`

The active rules are implemented in `legacy_importer/qc.py`. Empty threshold
dictionaries remain in YAML for typed configuration compatibility.

---

## 6. Manifest behavior

The manifest must contain:

```text
sample_name,full_path,organization_code,legacy_batch_code
```

Interpretation:

| Column | Meaning |
|---|---|
| `sample_name` | Sample accession/name used in sample IDs and database fields. |
| `full_path` | Local sample directory containing reads and old outputs. |
| `organization_code` | Organization code used for create-once/reuse behavior. |
| `legacy_batch_code` | Legacy batch/group key. |

`level_2` is omitted because its source values were only `-` and carried no
meaningful information.

CLI filters can select rows by:

- Organization
- Sample name
- Maximum row count

The complete normalized row is stored in the sample's `source_metadata` JSON.

---

## 7. File discovery behavior

`discovery.py` scans each sample directory.

### Raw reads

It searches `rawdata/` for:

```text
*_R1.fastq.gz
*_R2.fastq.gz
```

If multiple files match the same read type, discovery raises an error instead
of guessing. Missing R1 or R2 files are reported in inventory output.

### Tool outputs

Tool files are discovered using the glob patterns in YAML. Paths are
deduplicated and sorted. Discovery and extractor input selection happen
automatically when the importer reaches the manifest row's `full_path`.

| Tool | Discovered files | Parsed input |
|---|---|---|
| FastQC | `fastqc/*_fastqc.zip` | `fastqc_data.txt` extracted from each ZIP |
| BBMap | `*_bb_stats.txt`, `*_bb_hist.txt`, `*_ihist.txt`, `logs/bbmap_*.log` | BBMap summary log |
| Bracken | Bracken and Kraken report files | Prefer `*_kraken_report_bracken.txt` |
| MLST | `*_seemann_mlst_output.txt` | Same file |
| QUAST | `*_quast_report.tsv` | Same file |
| AMRFinder | Report TSV and organism text | Report TSV |
| Prokka | `prokka/*` | GFF/GFF3 |
| Mash sketch | `mash_k21_s1000/*.msh` | Storage only |
| SKA sketch | `ska_k15/*.skf` | Storage only |
| Export summary | Generated from normalized results | Storage only |

Parser discovery and GCP storage are separate policies. All configured inputs
are parsed, but only files matching a tool's `gcp_inputs` patterns are
archived. The current policy uploads `prokka/*`,
`mash_k21_s1000/*.msh`, and `ska_k15/*.skf`. Sketch files are optional and
activate automatically when present.

### Supplemental files

Discovery also reports files that are useful for inventory but are not
currently imported as dedicated configured tool artifacts:

- `*_quality_report.html`
- `*_summary.xlsx`
- Non-BBMap files matching `logs/*.log`

These appear under `supplemental_files` in inventory reports.

---

## 8. GCS object layout

### Raw reads

Raw reads use:

```text
gs://{raw_reads_bucket}/legacy_import/raw_reads/
    {organization_code}/{legacy_batch_code}/{sample_name}/
    {original_filename}
```

Each R1/R2 FASTQ is uploaded directly with its original `.fastq.gz` filename.
The reads row's `uri`, `size_bytes`, and `checksum` describe that uploaded
source file. The exact prefix can be changed in YAML.

### Artifacts

Artifact archives use:

```text
gs://{artifact_bucket}/{run_id}/{sample_id}/{task_id}.tar.gz
```

All path components are deterministic, so a retry targets the same object.

Artifact archives are created for all Prokka outputs, optional Mash/SKA sketches, and
the generated summary CSV. FastQC, BBMap, Bracken, MLST, QUAST, AMRFinder, and
Prokka GFF inputs are parsed into database rows but are not uploaded to GCP.

The summary CSV is assembled from normalized extractor results already held
by the importer: Bracken taxonomy, MLST, QUAST, BBMap, annotation/AMR counts,
and QC1/QC2 decisions. It is registered under the synthetic
`export_summary` task and does not depend on `sample_summary_view`.

### Archive contents

Files inside an artifact archive are stored below an `output/` root:

```text
output/
├── top-level-result.txt
└── prokka/
    └── sample.gff
```

The archive implementation:

- Rejects files outside the sample directory.
- Preserves paths relative to the sample directory.
- Sorts members deterministically.
- Clears variable user/group metadata.
- Sets timestamps to zero.
- Uses a deterministic gzip header timestamp.

This makes archives reproducible for unchanged source files.

---

## 9. PostgreSQL behavior

### Transactions

Each sample is imported as one explicit database transaction.

On success:

```text
commit
```

On any exception:

```text
rollback
```

This prevents a partially written sample from being committed.

### Upserts

Database writes use:

```sql
INSERT ...
ON CONFLICT (...) DO UPDATE
```

Before constructing an upsert, the importer reads the actual columns from
`information_schema.columns`. Fields absent from a deployed table are removed
from the row.

This permits minor differences between database deployments, but required
columns and tables are still checked by `inspect-schema`.

### JSON and UUID adaptation

- Dictionaries and lists are sent using PostgreSQL JSON adaptation.
- UUID objects are converted to strings for psycopg2.

---

## 10. Extractor integration

The extractor package is installed separately and pinned in `pyproject.toml`:

```text
bio-extractors==0.1.11
```

Adapters import the package lazily and use its public API:

```python
from bio_extractors import run_extractor

result = run_extractor("mlst", input_path)
```

The configured integrations are:

| Tool | Adapter behavior |
|---|---|
| FastQC | Safely extract `fastqc_data.txt`, then call `run_extractor("fastqc", ...)` |
| MLST | Call `run_extractor("mlst", path)` |
| QUAST | Call `run_extractor("quast", report_tsv)` |
| BBMap | Prefer `logs/bbmap_*.log` and call `run_extractor("bbmap", log)` |
| Bracken | Select the best hierarchical report and call `run_extractor("bracken", path)` |
| AMRFinder | Select the report TSV and call `run_extractor("amrfinder", path)` |
| Prokka | Select GFF/GFF3 and call `run_extractor("prokka", path)` |

Missing packages, unregistered tools, invalid files, and unexpected return
types are wrapped in `ExtractorError` with tool and path context.

Extractor output is treated as parsed data only. The importer adds:

- Result primary key
- `sample_id`
- `task_id`
- `read_id` and `read_type` for FastQC
- `created_at`

The result primary key is derived from the table name, sample ID, task ID,
source path, and a row-specific value such as `id`, `locus_tag`, `name`,
`gene`, or row index.

---

## 11. QC calculation

QC is calculated in this repository rather than extracted from old artifacts.

### QC1

QC1 requires both R1 and R2 FastQC rows. It validates equal supported read
lengths (`125`, `150`, `250`, or `300`) and calculates small- and large-genome
coverage, worst-pair duplication and N content, average GC content, and
read-count balance.

| Metric | PASS | WARN | FAIL |
|---|---|---|---|
| Small-genome coverage | `>= 30` | `20–<30` | `< 20` |
| Large-genome coverage | `>= 20` | `10–<20` | `< 10` |
| Duplication | `< 0.30` | `0.30–<0.50` | `>= 0.50` |
| N content | `< 0.01` | `0.01–<0.05` | `>= 0.05` |
| GC content | `0.25–0.75` | — | outside range |
| Read balance | `>= 0.90` | `0.80–<0.90` | `< 0.80` |

### QC2

QC2 requires BBMap summary metrics and QUAST assembly metrics.

| Metric | PASS | WARN | FAIL |
|---|---|---|---|
| Mean coverage | `>= 30` | `20–<30` | `< 20` |
| Percent mapped | `>= 98` | `95–<98` | `< 95` |
| Reference breadth | `>= 95` | `90–<95` | `< 90` |
| Total length | `1.5–8 Mb` | `1–<1.5 Mb` or `>8–10 Mb` | `<1 Mb` or `>10 Mb` |
| Contigs | `<= 250` | `251–500` | `> 500` |
| Largest contig | `>= 100 kb` | `50–<100 kb` | `< 50 kb` |
| N50 | `>= 20 kb` | `10–<20 kb` | `< 10 kb` |
| GC percentage | `25–75` | — | outside range |

Fractional QUAST GC values are converted to percentages before evaluation.

### Decisions

- `FAIL`: at least one metric fails.
- `WARN`: no metric fails and at least one metric warns.
- `PASS`: every metric passes.

QC IDs include the gate version. Changing a gate version creates a distinct,
deterministic QC record rather than silently replacing another version's ID.

If extractors are skipped, the importer can load existing result rows from the
database and recalculate QC from paired FastQC and BBMap/QUAST rows.

---

## 12. CLI command reference

The console command is installed through `pyproject.toml`:

```text
legacy-import
```

### `test-db`

Purpose:

- Connect to PostgreSQL.
- Run `SELECT current_database(), current_user, now()`.
- Inspect required schema.
- Optionally perform a rolled-back write test with `--write-test`.

No password is printed.

### `test-gcp`

Purpose:

- Authenticate to GCS.
- Create a tiny healthcheck object in each configured bucket.
- Confirm the object exists.
- Delete the object.
- Confirm deletion.

### `test-connections`

Runs both database and GCS checks and prints one combined JSON report.

### `inspect-schema`

Reads PostgreSQL's `information_schema` and reports:

- Missing core tables
- Missing core columns
- Missing result tables
- Missing annotation table

The command exits with status 1 if validation fails.

### `inventory`

Reads the manifest and scans local directories only.

It does not connect to PostgreSQL or GCS.

Output includes:

- Source manifest fields
- Whether the sample directory exists
- R1 and R2 paths
- Available tools
- Tool file lists
- Supplemental quality report, summary, and log files
- Missing read indicators

Output may be printed as JSON or written as CSV/JSON depending on the output
filename.

### `dry-run`

The dry run:

- Connects to PostgreSQL only to validate schema.
- Reads and filters the manifest.
- Scans local files.
- Computes deterministic IDs.
- Displays planned read destinations.
- Displays planned tasks and artifacts.
- Displays planned extractor calls and QC gates.

It does not upload files or commit database changes.

### `import`

Imports selected samples.

Important options:

| Option | Effect |
|---|---|
| `--limit` | Import at most this many selected rows. |
| `--organization` | Restrict to one `organization_code`. |
| `--sample-name` | Restrict to one sample. |
| `--resume` | Continue to later samples after a sample failure. |
| `--skip-existing` | Skip a sample if its deterministic sample ID already exists. |
| `--skip-reads` | Do not upload/upsert reads; existing read rows are loaded for FastQC references. |
| `--skip-artifacts` | Do not create/upload artifact archives; tasks and extractor results may still be imported. |
| `--skip-extractors` | Do not call extractors or insert new result rows. |
| `--skip-qc` | Do not calculate or upsert QC rows. |

Without `--resume`, processing stops after the first sample failure. With
`--resume`, failures are collected and processing continues.

The command exits with status 1 if any selected sample failed.

### `verify`

For each selected sample, verification checks:

- Organization exists by organization code.
- Sample exists.
- Reads exist.
- Synthetic tasks exist.
- Artifacts exist.
- QC1 and QC2 rows exist.
- Result row counts by configured tool/table.
- Referenced read and artifact GCS objects exist.

The report marks a sample as broadly successful when its organization and
sample exist and all referenced GCS objects exist. Detailed counts remain
available for stricter operational review.

---

## 13. File-by-file explanation

### Root files

#### `pyproject.toml`

Defines package metadata, Python requirements, dependencies, test extras, and
the `legacy-import` console entry point.

Runtime dependencies:

- Typer
- Pydantic
- PyYAML
- psycopg2-binary
- google-cloud-storage
- python-dotenv
- bio-extractors 0.1.11

It also configures pytest to search the `tests/` directory.

#### `README.md`

Provides concise operational instructions:

- Installation
- Database and GCP environment variables
- Connection tests
- Inventory
- Dry run
- Single-sample and full imports
- Extractor integration
- Verification
- Unit tests

#### `.env.example`

Shows the environment-variable names and example formats without containing
real credentials. It is documentation only and must not be populated with
committed secrets.

#### `.gitignore`

Excludes `.env`, `.secrets/`, Python caches, pytest temporary directories, and
editable-install metadata. The local service-account JSON is stored below
`.secrets/` so it is available to the importer without being tracked.

#### `configs/legacy_import.yaml`

Contains the default import policy:

- Credential variable names
- Bucket names
- Organization display names
- Legacy ownership templates
- Sample/read/task/artifact statuses
- Tool discovery patterns
- Artifact categories
- Result-table override for Prokka
- QC1 and QC2 enable flags and gate versions

### Package files

#### `legacy_importer/__init__.py`

Marks the directory as a Python package and exposes package version `0.1.0`.

#### `legacy_importer/cli.py`

Defines the Typer application and all required CLI commands.

It is responsible for:

- Parsing command-line options
- Loading configuration and manifests
- Applying row filters
- Formatting reports as JSON
- Writing inventory CSV/JSON files
- Managing top-level database connections
- Calling per-sample orchestration
- Collecting readable import failures
- Returning non-zero exit codes when validation or import fails

#### `legacy_importer/config.py`

Defines Pydantic models for the YAML configuration:

- `DatabaseConfig`
- `GCPConfig`
- `ToolConfig`
- `QCGateConfig`
- `QCConfig`
- `ImportConfig`

It also resolves the database URI and organization display name and loads
`.env` values before validating YAML.

#### `legacy_importer/ids.py`

Owns all deterministic UUID5 generation. No imported database entity uses a
random UUID.

#### `legacy_importer/manifest.py`

Validates manifest columns and rows with Pydantic. It normalizes optional
metadata, preserves source metadata, and implements organization, sample, and
limit filters.

#### `legacy_importer/schema.py`

Defines required core tables and columns and expected result tables.

It queries `information_schema.columns` and returns a structured report instead
of modifying the database schema.

#### `legacy_importer/db.py`

Provides:

- PostgreSQL connection creation
- Explicit transaction context management
- Schema-aware idempotent upserts
- Sample-scoped result row fetching
- Database health and rollback-write testing

#### `legacy_importer/gcs.py`

Provides:

- GCS client creation
- File upload
- GCS object existence checks
- Bucket health tests

Upload metadata includes URI, size, and a GCS checksum where available.

#### `legacy_importer/discovery.py`

Finds reads, configured tool outputs, and supplemental files. It returns a
`SampleInventory` object and detects ambiguous multiple-read matches.

#### `legacy_importer/archive.py`

Calculates local SHA-256 checksums and creates reproducible tool artifact
archives under an `output/` root.

#### `legacy_importer/import_ownership.py`

Builds and upserts:

- Organization
- Legacy import user
- Batch
- Job
- Run
- Sample

It also reuses an existing organization found by `organization_code`.

#### `legacy_importer/import_reads.py`

For each R1/R2 FASTQ file:

1. Builds its stable GCS object path.
2. Calculates the source file's SHA-256 checksum.
3. Uploads the original `.fastq.gz` when enabled.
4. Builds a deterministic reads row.
5. Upserts the row by `read_id`.

Raw reads are not treated as artifacts and are not wrapped in tar archives.

#### `legacy_importer/import_artifacts.py`

Handles selected GCP artifact processing in the required order:

1. Create/upsert the synthetic tool task.
2. Create archive.
3. Upload archive.
4. Read uploaded metadata.
5. Upsert final artifact row.

With the current policy this function handles complete Prokka output sets,
optional Mash/SKA sketches, and generated summary CSVs.

The final artifact row is not written before successful upload.

#### `legacy_importer/import_results.py`

Contains:

- Lazy extractor adapters
- Extractor error handling
- Tool-to-parser mapping
- Tool-to-result-primary-key mapping
- Safe FastQC ZIP extraction
- Tool-specific parser input selection
- FastQC read-ID and read-type resolution
- Extractor field normalization
- Result enrichment and upsert

This file isolates `bio_extractors.run_extractor` and file-format selection
from the rest of the importer.

#### `legacy_importer/qc.py`

Implements the paired-read QC1 and BBMap/QUAST QC2 rule sets. It also retains
the generic minimum/maximum evaluator for compatibility. QC rows contain
deterministic IDs, decisions, metric values, status maps, human-readable
reasons, and timestamps.

#### `legacy_importer/import_sample.py`

This is the central orchestration module.

For one manifest row it:

- Discovers source files.
- Builds ownership rows.
- Creates a GCS client only when uploads are required.
- Starts one database transaction.
- Imports ownership.
- Imports or reuses reads.
- Imports tasks, artifacts, and results for each discovered tool.
- Calculates QC.
- Returns a structured sample report.

#### `legacy_importer/verify.py`

Queries imported database counts, retrieves read/artifact URIs, checks GCS
object existence, and returns a structured post-import verification report.

#### `legacy_importer/logging_config.py`

Configures concise console logging and redacts passwords in URL-like error
messages before they are printed in failure reports.

### Test files

#### `tests/test_ids.py`

Confirms:

- UUIDs are deterministic.
- Different entities and keys produce different IDs.
- Read and result disambiguation keys are effective.

#### `tests/test_manifest.py`

Confirms:

- Valid manifest rows parse correctly.
- `-` is normalized to missing optional metadata.
- Required columns are enforced.

#### `tests/test_discovery.py`

Confirms:

- R1 and R2 discovery.
- Configured FastQC, BBMap-log, and Quast discovery.
- Supplemental quality-report discovery.
- Missing sample directories are reported.

#### `tests/test_archive.py`

Confirms:

- Archive paths are stored below `output/`.
- Nested directory structure is preserved.
- Files outside the sample directory are rejected.
- SHA-256 checksums are generated.

#### `tests/test_qc.py`

Confirms:

- QC1 PASS/WARN/FAIL behavior and paired-read requirements.
- QC1 compatibility with legacy percentage values.
- QC2 PASS/WARN/FAIL boundaries.
- QC2 required-source and numeric-field validation.
- Deterministic QC identifiers.

#### `tests/test_import_results.py`

Confirms:

- FastQC ZIP extraction and fraction preservation.
- Tool-specific parser input selection.
- BBMap log preference.
- Actionable missing-package errors.

#### `tests/test_import_ownership.py`

Confirms finalized manifest ownership mapping, deterministic batch/run
accessions, and private sample visibility.

#### `tests/test_import_reads.py`

Confirms raw FASTQs are uploaded directly with stable object names and their
original filenames.

#### `tests/test_schema.py`

Uses fake database cursors to confirm:

- Missing required columns are reported.
- A complete required schema passes validation.

The current suite contains 40 tests.

---

## 14. Safety and rerun guarantees

The repository implements these safeguards:

- Deterministic UUID5 values for imported entities.
- Stable GCS object names.
- Database `ON CONFLICT` upserts.
- Organization reuse by `organization_code`.
- One explicit database transaction per sample.
- Artifact database rows written only after successful upload.
- Dry-run mode without writes or uploads.
- Inventory mode without database or GCS access.
- Read ambiguity detection.
- Archive path containment checks.
- Password redaction in logs and error reports.
- No credentials stored in source files.
- Resume mode with per-sample failure reports.

---

## 15. Current integration assumptions and limitations

These points require confirmation against the real deployment before a
production migration:

1. **Python environment**
   Run the importer in an environment containing `bio-extractors==0.1.11`.
   Validation used `dbt_env` with editable source
   `C:\Users\User\Desktop\gitlab_repo\extractors` at commit `6e3a33b`.

2. **Result table columns**
   Result rows are filtered to columns that exist in each deployed table.
   Deployed tables must include every extractor field that should be retained.

3. **Conflict constraints**
   PostgreSQL must have unique or primary-key constraints matching the chosen
   conflict columns, normally the deterministic primary key.

4. **FastQC row-to-read matching**
   The importer resolves R1/R2 from the FastQC ZIP path and persists both
   `read_id` and `read_type`.

5. **QC prerequisites**
   QC1 requires both FastQC rows. QC2 requires the BBMap summary log and QUAST
   metrics. Missing required data raises an explicit runtime error.

6. **Supplemental outputs**
   Quality-report HTML, summary spreadsheets, and logs are inventoried but are
   not currently assigned dedicated artifact tasks unless added as configured
   tools.

7. **Verification strictness**
   Verification reports all result and QC counts, but its top-level `ok` value
   primarily checks organization, sample, and referenced GCS object existence.
   Operational acceptance may require stricter count rules.

8. **GCS rollback**
   A database rollback cannot roll back files already uploaded to GCS.
   Deterministic names make those objects safe for retry, but abandoned objects
   require a separate cleanup policy if desired.

---

## 16. Recommended production-readiness sequence

Before running a full migration:

1. Activate `dbt_env` and verify `bio-extractors==0.1.11`.
2. Confirm the extractor registry contains `amrfinder` and `prokka`.
3. Run `legacy-import test-db`.
4. Run `legacy-import inspect-schema`.
5. Resolve every missing table or column.
6. Run `legacy-import test-gcp`.
7. Run inventory for the complete manifest.
8. Review missing and ambiguous reads.
9. Run a dry run for several representative samples.
10. Import one sample with all features enabled.
11. Run verification and manually compare source files, GCS objects, and
    database rows.
12. Compare QC1/QC2 decisions with the expected production rules.
13. Import a small batch with `--resume`.
14. Rerun the same batch to prove idempotency.
15. Begin the complete migration with retained JSON failure reports.

---

## 17. Common commands

```bash
pip install -e ".[test]"
pytest

legacy-import test-connections --config configs/legacy_import.yaml
legacy-import inspect-schema --config configs/legacy_import.yaml

legacy-import inventory \
  --csv samples.csv \
  --config configs/legacy_import.yaml \
  --out inventory.csv

legacy-import dry-run \
  --csv samples.csv \
  --config configs/legacy_import.yaml \
  --limit 5

legacy-import import \
  --csv samples.csv \
  --config configs/legacy_import.yaml \
  --sample-name F1S1R3D2B2P3G09

legacy-import import \
  --csv samples.csv \
  --config configs/legacy_import.yaml \
  --resume

legacy-import verify \
  --csv samples.csv \
  --config configs/legacy_import.yaml
```

---

## 18. Final status

The repository is a complete import framework with the requested package
layout, CLI surface, deterministic identity model, PostgreSQL and GCS
integration, extractor boundary, QC engine, verification tools, documentation,
and unit tests.

The example directory `F1S1R3D2B2P3G09` was validated under `dbt_env` with
`bio-extractors==0.1.11`:

| Integration | Result |
|---|---|
| FastQC | 2 rows |
| BBMap | 1 row |
| Bracken | 1 row |
| MLST | 1 row |
| QUAST | 1 row |
| AMRFinder | 8 rows |
| Prokka | 5,058 rows |
| QC1 | `WARN` due to elevated duplication |
| QC2 | `PASS` |
| Unit tests | 40 passed |

The remaining work is deployment-specific: confirming database result-table
columns and constraints, credentials, buckets, and production migration
policy.
