#!/usr/bin/env python3
import json
import os
import sys

sys.path.insert(0, "/data/datahub/scripts")
from blf_datahub_mcp.datahub_client import DataHubClient
from blf_datahub_mcp.field_lineage import _extract_schema_field_names
from blf_datahub_mcp.hive import make_hive_dataset_urn

table = sys.argv[1] if len(sys.argv) > 1 else "data_sec_dw.dim_store_info"
urn = make_hive_dataset_urn(table)
client = DataHubClient(os.environ["DATAHUB_GMS_URL"], token=os.environ.get("DATAHUB_GMS_TOKEN"))
payload = client.get_schema_metadata(urn)
print("top_keys", sorted(payload.keys()))
print("field_count", len(_extract_schema_field_names(payload)))
print("sample", json.dumps(payload, ensure_ascii=False)[:1200])
