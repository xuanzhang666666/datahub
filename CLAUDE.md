# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

@AGENTS.md

---

## xander/python/job_info_sync_datahub — Custom Lineage Sync Module

This is a standalone Python package (not managed by Gradle) for syncing scheduling job lineage
and metadata into DataHub. It lives in `xander/python/job_info_sync_datahub/` and is deployed
independently to internal servers via FTP (`put2`/`get2`).

### Running Tests

From `xander/python/` as working directory:

```bash
# Default gate (no trino required) — must pass before uploading to neo4j2
sh scripts/run_job_info_sync_datahub_tests.sh

# Full suite (requires `import trino`)
FULL_TESTS=1 sh scripts/run_job_info_sync_datahub_tests.sh

# Single test file
cd xander/python
export PYTHONPATH=.
python -m pytest job_info_sync_datahub/tests/test_audit_datahub_table_lineage_quality.py -q
```

The script auto-detects Python at `/opt/anaconda3/bin/python`, `~/anaconda3/bin/python`, or
`python3`. Override with `LINEAGE_PYTHON=/path/to/python`.

### Architecture

Data flows through five stages:

1. **Scheduling metadata** — `schedule_client` fetches `shell_command`, `job_display_name`, etc. from DMP.
2. **Runtime parsing** — `runtime_parser.parse_runtime_context()` extracts GitLab repo name and job file path from the shell command. Handles `$VAR`/`${VAR}` path suffixes, non-ASCII whitespace, and scheduling tokens (`prod`/`before`/`after`/single-letter partitions).
3. **ETL script resolution** — `etl_file_resolver` + `gitlab_client` fetch script content from GitLab or `BLF_ETL_LOCAL_ROOT`.
4. **SQL / lineage parsing** — `lineage_parser`, `lineage_vote`, and optionally LLM (`field_lineage_llm`, `lineage_llm_compare`) produce `TableLineage` / `FieldLineage`.
5. **DataHub write** — `datahub_writer` emits `structuredProperties` via OpenAPI PATCH and `upstreamLineage` via the acryl-datahub SDK MCP.

**tmp_* table contract**: Two-stage SQL jobs (CREATE/INSERT into `tmp_*`, then write to a real table) must have the tmp tables stripped from lineage. The LLM system prompt in `lineage_llm_compare.py` enforces this; the contract is tested in `test_two_stage_tmp_lineage_contract.py`.

### Key Files

| File | Purpose |
|------|---------|
| `models.py` | Core dataclasses: `TableLineage`, `FieldLineage`, `ParseConfidence`, `ParseStatus` |
| `runtime_parser.py` | Shell command → `RuntimeContext` (GitLab name, job path, job type) |
| `etl_file_resolver.py` | ETL script content resolution (GitLab + local fallback) |
| `datahub_writer.py` | Writes structuredProperties and upstreamLineage to DataHub |
| `hive_table_existence.py` | Validates Hive table existence via Trino `information_schema` |
| `hive_fqtn_validation.py` | Pre-write FQTN validation rules (format, DB whitelist, prefix, `_` count) |
| `lineage_write_policy.py` | Orchestrates LLM lineage post-processing, validation, and audit fields |
| `audit_datahub_table_lineage_quality.py` | Read-only lineage quality audit (scans DataHub + Hive) |
| `audit_missing_dataset_properties.py` | Scans datasets missing structured properties (ETL script, etc.) |
| `manual_upstream_lineage.py` | CLI tool to manually add/replace upstream lineage edges |
| `batch_sync.py` | Batch lineage sync pipeline with JSONL checkpoint/resume |
| `sync_job_lineage.py` | Single-job lineage sync entry point (used by `batch_sync`) |
| `structured_properties.py` | DataHub structured property URN constants |
| `field_lineage_*.py` | Field-level lineage: models, reader, writer, LLM, Excel export |
| `table_documentation_*.py` | Table documentation generation and batch sync |
| `table_documentation_full_discovery.py` | Discovers datasets with structured ETL properties for doc generation |
| `table_lineage_from_dataset_props.py` | Builds table lineage from existing dataset structured properties |

### Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `DATAHUB_GMS_URL` | `http://localhost:8080` | DataHub GMS endpoint |
| `DATAHUB_GMS_TOKEN` | — | Auth token |
| `BLF_DATAHUB_PLATFORM_INSTANCE` | `blf-prod-hive` | Hive platform instance |
| `DATAHUB_MYSQL_MODE` | `host` | `host` or `docker` for MySQL access |
| `DATAHUB_MYSQL_HOST/PORT/USER/PASSWORD/DATABASE` | localhost defaults | MySQL connection |
| `DATAHUB_MYSQL_CONTAINER` | `datahub-mysql-1` | Docker container name (docker mode) |
| `BLF_ETL_LOCAL_ROOT` | — | Local mirror of ETL scripts |
| `LINEAGE_PYTHON` | auto-detected | Python interpreter override |

**Audit script (`run_audit_datahub_table_lineage_quality.sh`) additional vars:**

| Variable | Default | Purpose |
|----------|---------|---------|
| `REPORT_DIR` | `$WORKSPACE/lineage_quality_reports` | Output directory for JSONL/XLSX reports |
| `LINEAGE_QUALITY_QUERY` | `*` | DataHub search query to filter datasets |
| `MAX_DATASETS` | `0` (unlimited) | Cap on number of datasets to scan |
| `BATCH_SIZE` | `2000` | DataHub scroll batch size |
| `HIVE_CHUNK_SIZE` | `1000` | Hive `information_schema` query chunk size |
| `CHECK_NO_UPSTREAM` | `0` | Set to `1` to flag non-ODS tables with no upstream lineage |
| `DATAHUB_MYSQL_BIN` | auto-detected | Path to `mysql` binary on the host |

### Deployment

After tests pass, package and upload via FTP:

```bash
# Create tarball
tar -czf job_info_sync_datahub.tar.gz job_info_sync_datahub/

# Upload to internal server (see xander/README.md for put2/get2 details)
put2 job_info_sync_datahub.tar.gz
```

**Never upload if tests fail.**
