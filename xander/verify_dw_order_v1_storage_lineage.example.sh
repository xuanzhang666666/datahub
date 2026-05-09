#!/usr/bin/env sh
# After hive-metastore ingest with emit_storage_lineage, check upstream lineage edges (storage -> table).
#
#   export DATAHUB_GMS_URL=http://127.0.0.1:8080   # or init via: datahub init
#   sh xander/verify_dw_order_v1_storage_lineage.example.sh
set -e
URN_DEFAULT='urn:li:dataset:(urn:li:dataPlatform:hive,blf-prod-hive.default.dw_order_v1,PROD)'
URN="${DATASET_URN:-$URN_DEFAULT}"

if ! command -v datahub >/dev/null 2>&1; then
  echo "datahub CLI not found. Install acryl-datahub and ensure datahub is on PATH." >&2
  exit 2
fi

echo "Upstream lineage for:" "$URN"
exec datahub lineage --urn "$URN" --direction upstream --format json
