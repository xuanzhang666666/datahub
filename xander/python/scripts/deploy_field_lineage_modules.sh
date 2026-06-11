#!/usr/bin/env bash
# neo4j2: get2 flat filenames from FTP, then copy into /data/datahub/scripts/
set -euo pipefail
DEST=/data/datahub/scripts/job_info_sync_datahub
SCRIPTS=/data/datahub/scripts
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
  get2 "$f"
  cp "/root/$f" "$DEST/$f"
  echo "deployed $f"
done
for f in \
  run_field_lineage_export_to_excel.sh \
  run_field_lineage_import_to_datahub.sh \
  run_field_lineage_phase2_reexport.sh
do
  get2 "$f"
  cp "/root/$f" "$SCRIPTS/$f"
  chmod +x "$SCRIPTS/$f"
  echo "deployed script $f"
done
