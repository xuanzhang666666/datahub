#!/bin/sh
# Run on neo4j2 (GMS at http://127.0.0.1:8080). Flat name for put2/get2.
GMS="${DATAHUB_GMS_URL:-http://127.0.0.1:8080}"
curl -sS -m 120 -X POST "$GMS/api/graphql" \
  -H 'Content-Type: application/json' \
  -d '{"query":"query { s: searchAcrossEntities(input: { types: [CONTAINER], query: \"data_drink\", start: 0, count: 8 }) { total searchResults { entity { urn type ... on Container { properties { name } subTypes { typeNames } dataPlatformInstance { instanceId urn } browsePathV2 { path { name entity { urn } } } parentContainers { count containers { urn } } } } } } r: browseV2(input: { types: [CONTAINER], path: [\"hive\"], start: 0, count: 30 }) { total start count groups { name count hasSubGroups entity { urn ... on Container { properties { name } browsePathV2 { path { name } } } } } } }"}'
