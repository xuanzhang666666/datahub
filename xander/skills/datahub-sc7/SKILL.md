---
name: datahub-sc7
description: Use this skill whenever the user asks to back up, snapshot, restore, deploy, validate, or synchronize the BLF DataHub Docker Compose service between neo4j2 and sc7, mentions DataHub cold standby, cold snapshot, tar.gz snapshot, HDFS backup package, recovery SOP, sc7 cold backup, neo4j2 DataHub backup, or the custom DataHub lineage UI where task dependency edge conditions must display on the graph.
---

# DataHub sc7 Cold Backup And Lineage UI Operations

Use this skill for BLF/Wormpex DataHub operations on:

- Production source: `neo4j2.dp.data.bj1.wormpex.com`
- Cold standby target: `sc7.dp.data.bj1.wormpex.com`
- DataHub directory: `/data/datahub`
- Frontend port: `9002`
- GMS port: `8080`
- Bastion entry: `agent@10.253.40.11:7233`

Always use the `agent-bastion` skill for remote commands.

## Choose The Right Reference

- For sc7 cold-standby data synchronization from neo4j2, read `SYNC-BACKUP.md`.
- For a full cold snapshot tar package with recovery SOP, read `COLD-SNAPSHOT-RESTORE.md`.
- For fresh sc7 deployment or image transfer, read `DEPLOY.md`.
- For task lineage edge condition labels missing from the UI, read `LINEAGE-CONDITION-UI.md`.
- For compose differences on sc7, inspect `docker-compose.yml`.

Load only the reference needed for the user request.

## Operating Rules

1. Confirm the target host before destructive work.
2. Before stopping or replacing DataHub, capture:
   - `docker compose -f /data/datahub/docker-compose.yml ps`
   - `docker ps --filter name=datahub`
   - `df -h /data`
   - current frontend image tag from `/data/datahub/docker-compose.yml`
3. Back up `/data/datahub/docker-compose.yml` before editing it:
   `cp /data/datahub/docker-compose.yml /data/datahub/docker-compose.yml.bak.<reason>.$(date +%Y%m%d%H%M%S)`
4. For cold snapshots, stop the compose stack before archiving `/data/datahub`.
5. Use `tar --numeric-owner` for filesystem snapshots so container UID/GID ownership survives restore.
6. After any restore or frontend image change, verify:
   - GMS health: `curl -s -o /tmp/gms_health.out -w "%{http_code}" http://127.0.0.1:8080/health`
   - Frontend: `curl -s -o /tmp/datahub_frontend_home.html -w "%{http_code}" http://127.0.0.1:9002/`
   - container health via `docker ps` / `docker inspect`
7. Do not put FTP passwords, DataHub tokens, cookies, or signing keys into the skill, final answer, or new files. Use existing host-side `put2/get2` configuration or prompt the operator if a credential is genuinely missing.

## Snapshot Deliverable Expectations

When the user asks for a full snapshot package for HDFS or offline backup, produce or verify a directory under `/data/datahub_snapshots/<snapshot_name>/` containing:

- the DataHub archive, preferably `datahub_root.tar.gz` or `datahub_root.tar`
- `docker-compose.yml`
- `docker-images.txt`
- `compose-ps.before.txt`
- `df.before.txt`
- `datahub_root.tar*.md5`
- a recovery SOP, either copied from `COLD-SNAPSHOT-RESTORE.md` or generated as `RESTORE-SOP.md` with the concrete snapshot name

Restart the service after the snapshot and verify health before reporting completion.

## sc7 Consistency Expectations

When syncing sc7 from neo4j2:

- MySQL `metadata_aspect_v2` total row count and distinct URN count should match or differ only by writes during backup.
- Elasticsearch key index doc counts should be close, especially `datasetindex_v2`, `graph_service_v1`, `queryindex_v2`, and `system_metadata_service_v1`.
- Frontend and GMS on sc7 must return HTTP 200.
- If the current production frontend includes the lineage condition label fix, sc7 must run the same frontend image or an equivalent image verified by `LINEAGE-CONDITION-UI.md`.

## Reporting

Report concise evidence, not raw logs:

- snapshot path and size
- md5 path and value when available
- source and target image tags
- health check HTTP codes
- MySQL and Elasticsearch comparison summary
- any skipped step and why
