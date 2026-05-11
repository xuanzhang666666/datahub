#!/bin/sh
# Wrapper: canonical script is xander/run/run_hive_lineage_on_neo4j2.sh (repo root path kept for old references).
exec "$(dirname "$0")/xander/run/run_hive_lineage_on_neo4j2.sh" "$@"
