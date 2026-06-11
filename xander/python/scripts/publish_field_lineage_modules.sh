#!/usr/bin/env bash
# Local: flatten canonical sources and put2 to FTP (flat names required by get2 on neo4j2).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STAGE="${TMPDIR:-/tmp}/field_lineage_ftp_stage.$$"
mkdir -p "$STAGE"
trap 'rm -rf "$STAGE"' EXIT

for f in \
  field_lineage_excel.py \
  field_lineage_models.py \
  field_lineage_policy.py \
  field_lineage_writer.py \
  field_lineage_batch_summary.py \
  field_lineage_cli.py \
  field_lineage_llm.py \
  field_lineage_datahub_reader.py \
  field_lineage_urn_repair.py \
  repair_field_lineage_urns.py
do
  cp "$ROOT/job_info_sync_datahub/$f" "$STAGE/$f"
done
for f in \
  run_field_lineage_export_to_excel.sh \
  run_field_lineage_import_to_datahub.sh \
  run_field_lineage_phase2_reexport.sh \
  deploy_field_lineage_modules.sh
do
  cp "$ROOT/scripts/$f" "$STAGE/$f"
done

(
  cd "$STAGE"
  for f in *; do
    echo "put2 $f"
    put2 "$f"
  done
)
echo "done; on neo4j2: get2 deploy_field_lineage_modules.sh && bash /root/deploy_field_lineage_modules.sh"
