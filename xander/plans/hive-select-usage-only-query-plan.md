# Hive SELECT Usage-Only Query Plan

## Summary

Only historical Hive read queries should be used to improve DataHub usage metadata and the dataset `Queries` tab. The ingestion flow should process `SELECT` queries, publish usage and Query entities, and avoid writing any lineage or operation metadata.

The target output is:

- Dataset `datasetUsageStatistics`, including `topSqlQueries` and field usage where available.
- Deduplicated Query entities with `queryProperties`.
- Query-to-dataset or query-to-field `querySubjects`.
- Query-level `queryUsageStatistics`.

The flow must not emit:

- `upstreamLineage`
- `fineGrainedLineage`
- `operation`
- lineage edges from write statements

## Data Handling

- Input should be newline-delimited JSON, one query record per line.
- Required minimum field:
  - `query`: SQL text
- Recommended fields:
  - `timestamp`: execution timestamp; if missing, use the collection date.
  - `user`: executing user; if unavailable, leave empty or use a controlled placeholder such as `_unknown_hive_user`.
  - `session_id`: optional, useful if temporary-table resolution is later needed.
- Only retain read queries:
  - Keep `SELECT ...`
  - Keep `WITH ... SELECT ...`
  - Skip `INSERT`, `INSERT OVERWRITE`, `CREATE TABLE AS SELECT`, `CREATE VIEW`, `UPDATE`, `DELETE`, and `MERGE`.
- Normalize and aggregate before publishing:
  - Strip comments and non-semantic formatting differences.
  - Fingerprint normalized SQL.
  - Aggregate execution count, latest execution time, and user counts.
  - Keep only top queries per dataset, defaulting to top 20.

Example input:

```json
{"query":"SELECT spu_id, product_name FROM default.ods_bach_baseinfo_product_product_spu WHERE dt='20260603'","timestamp":1780502400,"user":"report_user"}
```

## Implementation Plan

Implement this as a minimal enhancement to `metadata-ingestion`'s `sql-queries` source.

Add config fields to `SqlQueriesSourceConfig`:

- `generate_lineage: bool = True`
- `generate_operations: bool = True`
- `generate_queries: bool = True`
- `generate_usage_statistics: bool = True`
- `generate_query_usage_statistics: bool = True`
- `select_only: bool = False`

Pass these config values into `SqlParsingAggregator` instead of the current hardcoded values:

- `generate_lineage`
- `generate_queries`
- `generate_usage_statistics`
- `generate_query_usage_statistics`
- `generate_operations`

When `select_only=true`, filter each `QueryEntry` before adding it to the aggregator:

- Parse the statement using the configured dialect, preferably `override_dialect`.
- Treat `SELECT` and top-level query expressions such as `WITH ... SELECT` as allowed.
- Treat parser failures conservatively: skip the query and increment a report counter instead of emitting uncertain metadata.
- Skip all mutation or DDL query types.

Add report counters:

- `num_non_select_queries_skipped`
- `num_select_only_parse_failures`

Recommended recipe:

```yaml
source:
  type: sql-queries
  config:
    query_file: /path/to/hive_select_usage_YYYYMMDD.jsonl
    platform: hive
    env: PROD
    override_dialect: hive
    use_schema_resolver: true
    generate_lineage: false
    generate_operations: false
    select_only: true
    usage:
      start_time: 2026-06-03T00:00:00Z
      end_time: 2026-06-04T00:00:00Z
      bucket_duration: DAY
sink:
  type: datahub-rest
  config:
    server: http://<gms-host>:8080
```

## Test Plan

Add focused unit tests for:

- Config defaults preserve existing behavior.
- Config can disable lineage and operations.
- `select_only=true` keeps `SELECT`.
- `select_only=true` keeps `WITH ... SELECT`.
- `select_only=true` skips `INSERT SELECT`, `INSERT OVERWRITE`, `CREATE TABLE AS SELECT`, `UPDATE`, `DELETE`, and `MERGE`.
- `generate_lineage=false` produces no `upstreamLineage`.
- Usage-only config still produces:
  - `QueryProperties`
  - `QuerySubjects`
  - `QueryUsageStatistics`
  - `DatasetUsageStatistics`

Add a small integration fixture containing mixed statements:

- 10 read queries.
- 5 write or DDL statements.

Acceptance checks:

- The target Hive dataset shows high-frequency read queries in the `Queries` tab.
- Dataset stats include query counts and top SQL queries.
- Lineage view is unchanged by this usage-only ingestion.
- No query entities are generated for skipped write statements.

## Assumptions

- The business goal is usage profiling, not lineage construction.
- Hive table schemas are already ingested into DataHub, so query subjects and field usage can be resolved.
- Raw historical query logs remain outside DataHub; only aggregated and representative SQL templates should be stored in the graph.
- Default top query retention is 20 per dataset unless a later implementation adds a configurable limit.
