#!/usr/bin/env sh
# BLF-side evidence for DataHub lineage planning (scheduling metadata + Hive DDL).
# Runs locally: prints SQL and ssh/beeline patterns. Does not connect to VPN/Trino/beeline by itself.
#
# Cross-skill flow: blf-schedule-job-info → blf-schedule-job-dependency → blf-hive-table-ddl → (optional) blf-hive-table-lineage
set -e
JOB="dw_order_v1"
DATE_HINT="replace_with_max_dt_from_ods"

cat <<EOF
=== 1) Trino: latest partition on ods schedule job table ===
SELECT max(dt) AS max_dt FROM default.ods_data_platform_dmp_schedule_job_basic_info;

=== 2) Trino: job row for ${JOB} (use max_dt as dt) ===
SELECT job_display_name, job_name, upstream_jobs, upstream_jobs_conditions,
       try(from_utf8(from_base64(shell_commond))) AS shell_command
FROM default.ods_data_platform_dmp_schedule_job_basic_info
WHERE dt = '${DATE_HINT}'
  AND job_display_name = '${JOB}';

(Run with your blf-hive-table-query / Python trino client; set TRINO_* env as needed.)

=== 3) Hive DDL: EXTERNAL + LOCATION (blf-hive-table-ddl / beeline on data1) ===
SHOW CREATE TABLE default.dw_order_v1

=== 4) Optional: Hive-table-to-Hive-table / column lineage ===
Run the standalone skill pipeline blf-hive-table-lineage (off-repo ~/.claude/skills/...)
with root job_display_name '${JOB}'. Map any resolved Hive refs to DataHub URNs:

  urn:li:dataset:(urn:li:dataPlatform:hive,blf-prod-hive.<db>.<table>,PROD)

EOF
