#!/usr/bin/env bash
# Neo4j2 (DataHub GMS) + FTP: batch restoreIndices for datasets whose ES browsePathV2
# lacked dataPlatformInstance (duplicate "blf-prod-hive" browse folder).
#
# Prerequisites: agent-bastion access to neo4j2.dp.data.bj1; put2/get2; flat FTP filename.
# Bastion: use FQDN neo4j2.dp.data.bj1 (not neo4j2). Avoid && in one remote line if denied.
#
# 1) From repo root: cp xander/scripts/gms-es/urns_hive_short_browsepath_batch.json ./u13.json
# 2) put2 u13.json
# 3) ssh ... "@neo4j2.dp.data.bj1 get2 u13.json"
# 4) ssh ... "@neo4j2.dp.data.bj1 mv u13.json /tmp/urns_batch_restore.json"
# 5) POST restore (omit aspectNames to restore all aspects for those URNs, or add query params if allowed):
#    ssh ... "@neo4j2.dp.data.bj1 curl -sS -m 300 -X POST http://127.0.0.1:8080/openapi/operations/elasticSearch/restoreIndices?batchSize=100 -H Content-Type:application/json --data-binary @/tmp/urns_batch_restore.json"
#
# After MAE consumer catches up (often 1–5 minutes), ES short-path count should drop to 0;
# browseV2 root should show a single blf-prod-hive bucket (instance URN).

exit 0
