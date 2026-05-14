#!/usr/bin/env python3
"""
Hard-delete all Hive entities (dataset + container) from DataHub.
Usage: python3 delete_hive_entities.py [--dry-run]
"""
import sys
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

GMS_URL = "http://127.0.0.1:8080"
PLATFORM = "hive"
DRY_RUN = "--dry-run" in sys.argv

try:
    from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
except ImportError as e:
    logger.error("Cannot import datahub SDK: %s", e)
    sys.exit(1)

graph = DataHubGraph(DatahubClientConfig(server=GMS_URL))

logger.info("Fetching all Hive entities from %s ...", GMS_URL)

all_urns = []
for entity_type in ["dataset", "container"]:
    try:
        urns = list(
            graph.get_urns_by_filter(
                entity_types=[entity_type],
                platform=f"urn:li:dataPlatform:{PLATFORM}",
            )
        )
        logger.info("  %s: %d entities", entity_type, len(urns))
        all_urns.extend(urns)
    except Exception as exc:
        logger.error("Error fetching %s: %s", entity_type, exc)

total = len(all_urns)
logger.info("Total: %d entities", total)

if total == 0:
    logger.info("Nothing to delete. Exiting.")
    sys.exit(0)

if DRY_RUN:
    logger.info("[DRY-RUN] Would delete:")
    for urn in all_urns:
        print(f"  {urn}")
    logger.info("[DRY-RUN] Would delete %d entities", total)
    sys.exit(0)

success = 0
failed = 0
for i, urn in enumerate(all_urns, 1):
    try:
        graph.delete_entity(urn=urn, hard=True)
        logger.info("[%d/%d] Deleted: %s", i, total, urn)
        success += 1
    except Exception as exc:
        logger.error("[%d/%d] Failed: %s -> %s", i, total, urn, exc)
        failed += 1

logger.info("Done. Success=%d  Failed=%d", success, failed)
if failed:
    sys.exit(1)
