"""DataHub DataJob writer for scheduler metadata."""

from __future__ import annotations

import html as _html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Protocol

from datahub.emitter.mce_builder import make_data_job_urn
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter
from datahub.metadata.schema_classes import (
    CorpUserInfoClass,
    DataJobInfoClass,
    DataJobInputOutputClass,
    EdgeClass,
    EditableDataJobPropertiesClass,
    OwnerClass,
    OwnershipClass,
    OwnershipTypeClass,
)

from .models import SchedulerJobDependency, SchedulerJobMetadata
from .trigger_parser import TRIGGER_TIMER

ORCHESTRATOR = "blf-schedule"
FLOW_ID = "blf-schedule"
ENV = "PROD"
JOB_TYPE = "BLF_SCHEDULE_JOB"
SCHEDULE_URL_TEMPLATE = "https://schedule.corp.bianlifeng.com/job/{job}"
URN_JOB_CONTENT_XML = "urn:li:structuredProperty:blf.data.schedule.job_content_xml"
URN_JOB_EXECUTE_SHELL = "urn:li:structuredProperty:blf.data.schedule.job_execute_shell"
EDGE_PROP_CONDITION = "blf_schedule_dependency_condition"
EDGE_PROP_STATUS = "blf_schedule_dependency_status"

_XML_DESCRIPTION_RE = re.compile(r"<description>(.*?)</description>", re.DOTALL | re.IGNORECASE)


def parse_job_xml_description(content: str) -> str:
    """Extract and clean the <description> text from a Jenkins job XML."""
    if not content:
        return ""
    m = _XML_DESCRIPTION_RE.search(content)
    if not m:
        return ""
    raw = _html.unescape(m.group(1))
    # Normalise \r\n and bare \r to \n
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    return raw.strip()


@dataclass(frozen=True)
class DataJobStructuredProperty:
    property_urn: str
    string_value: str


class EmitterLike(Protocol):
    def emit_mcp(self, mcp: MetadataChangeProposalWrapper) -> None:
        ...


EmitterFactory = Callable[[str, str | None], EmitterLike]


def default_emitter_factory(gms_url: str, token: str | None) -> EmitterLike:
    return DatahubRestEmitter(gms_url, token=token)


def make_scheduler_datajob_urn(job_display_name: str) -> str:
    return make_data_job_urn(
        orchestrator=ORCHESTRATOR,
        flow_id=FLOW_ID,
        job_id=job_display_name,
        cluster=ENV,
    )


def _iso(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.isoformat()


def serialize_job_dependencies(
    dependencies: list[SchedulerJobDependency],
) -> str:
    payload = [
        {
            "upstream": dep.upstream_job_display_name,
            "condition": dep.condition,
            "status": dep.status,
        }
        for dep in dependencies
    ]
    return json.dumps(payload, ensure_ascii=False)


def _trigger_type_value(metadata: SchedulerJobMetadata) -> str:
    if not metadata.trigger_types:
        return ""
    if TRIGGER_TIMER in metadata.trigger_types and len(metadata.trigger_types) > 1:
        return ",".join(metadata.trigger_types)
    return metadata.trigger_types[0]


def _custom_properties(metadata: SchedulerJobMetadata) -> dict[str, str]:
    return {
        "job_id": str(metadata.id),
        "job_name": metadata.job_name,
        "job_display_name": metadata.job_display_name,
        "trigger_type": _trigger_type_value(metadata),
        "cron_schedule": metadata.cron_schedule,
        "time_hour_param": metadata.time_hour_param,
        "scheduler_job_dependencies": serialize_job_dependencies(metadata.job_dependencies),
        "job_owner_name": metadata.job_owner_name,
        "job_proxy_user": metadata.job_proxy_user,
        "line_business_code": metadata.line_business_code,
        "contacts_name": metadata.contacts_name,
        "assigned_node": metadata.assigned_node,
        "job_disable": metadata.job_disable,
        "job_priority": metadata.job_priority,
        "build_keep_days": metadata.build_keep_days,
        "build_keep_num": metadata.build_keep_num,
        "upstream_jobs": ",".join(metadata.upstream_jobs),
        "upstream_jobs_conditions": metadata.upstream_jobs_conditions,
        "delay_config": metadata.delay_config,
        "ivr_notify": metadata.ivr_notify,
        "sms_notify": metadata.sms_notify,
        "im_notify": metadata.im_notify,
        "job_size": str(metadata.job_size),
        "job_count": str(metadata.job_count),
        "last_build_start_time": _iso(metadata.last_build_start_time),
        "created_time": _iso(metadata.created_time),
        "updated_time": _iso(metadata.updated_time),
        "build_update_time": _iso(metadata.build_update_time),
        "batch_exec_time": _iso(metadata.batch_exec_time),
        "schedule_url": SCHEDULE_URL_TEMPLATE.format(job=metadata.job_display_name),
    }


def build_datajob_info(metadata: SchedulerJobMetadata) -> DataJobInfoClass:
    description = (
        f"调度作业: {metadata.job_display_name}\n"
        f"负责人: {metadata.job_owner_name}\n"
        f"业务线: {metadata.line_business_code}\n"
        f"禁用状态: {metadata.job_disable}\n"
        f"调度链接: {SCHEDULE_URL_TEMPLATE.format(job=metadata.job_display_name)}"
    )
    return DataJobInfoClass(
        name=metadata.job_display_name,
        type=JOB_TYPE,
        description=description,
        customProperties=_custom_properties(metadata),
    )


def _wrap_shell_command(content: str) -> str:
    return f"```shell\n{content}\n```"


def _wrap_job_xml(content: str) -> str:
    return f"```xml\n{content}\n```"


def _owner_urn(owner_name: str) -> str:
    # DataHub uses all-lowercase "corpuser" in URNs (urn:li:corpuser:username)
    return f"urn:li:corpuser:{owner_name}"


def build_datajob_ownership(metadata: SchedulerJobMetadata) -> OwnershipClass | None:
    owner_name = (metadata.job_owner_name or "").strip()
    if not owner_name:
        return None
    return OwnershipClass(
        owners=[
            OwnerClass(
                owner=_owner_urn(owner_name),
                type=OwnershipTypeClass.TECHNICAL_OWNER,
            )
        ]
    )


def build_datajob_documentation(
    metadata: SchedulerJobMetadata,
) -> EditableDataJobPropertiesClass | None:
    description = parse_job_xml_description(metadata.content)
    if not description:
        return None
    return EditableDataJobPropertiesClass(description=description)


def build_corpuser_info(owner_name: str) -> CorpUserInfoClass:
    """Minimal CorpUser aspect — upserted before ownership to ensure the user exists."""
    return CorpUserInfoClass(active=True, displayName=owner_name, email="")


def build_datajob_structured_properties(
    metadata: SchedulerJobMetadata,
) -> list[DataJobStructuredProperty]:
    props: list[DataJobStructuredProperty] = []
    shell = (metadata.shell_command or "").strip()
    if shell:
        props.append(
            DataJobStructuredProperty(
                property_urn=URN_JOB_EXECUTE_SHELL,
                string_value=_wrap_shell_command(shell),
            )
        )
    xml = (metadata.content or "").strip()
    if xml:
        props.append(
            DataJobStructuredProperty(
                property_urn=URN_JOB_CONTENT_XML,
                string_value=_wrap_job_xml(xml),
            )
        )
    return props


def _structured_props_url(gms_base: str, datajob_urn: str) -> str:
    encoded = urllib.parse.quote(datajob_urn, safe="")
    return (
        f"{gms_base.rstrip('/')}/openapi/v3/entity/dataJob/"
        f"{encoded}/structuredProperties"
    )


def patch_datajob_structured_properties(
    gms_url: str,
    datajob_urn: str,
    props: list[DataJobStructuredProperty],
    token: str | None = None,
) -> None:
    if not props:
        return

    patch_ops = [
        {
            "op": "add",
            "path": f"/properties/{prop.property_urn}",
            "value": {
                "propertyUrn": prop.property_urn,
                "values": [{"string": prop.string_value}],
            },
        }
        for prop in props
    ]
    body = {
        "patch": patch_ops,
        "arrayPrimaryKeys": {"properties": ["propertyUrn"]},
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        _structured_props_url(gms_url, datajob_urn),
        data=data,
        method="PATCH",
        headers={
            "Content-Type": "application/json-patch+json",
            "Accept": "application/json",
        },
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            resp.read()
            if resp.status != 200:
                raise RuntimeError(
                    f"PATCH DataJob structuredProperties 返回 HTTP {resp.status}"
                )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"PATCH DataJob structuredProperties HTTP {exc.code}: {detail}"
        ) from exc


StructuredPropertiesPatcher = Callable[
    [str, str, list[DataJobStructuredProperty], str | None],
    None,
]


def build_input_datajob_edge(dep: SchedulerJobDependency) -> EdgeClass:
    properties: dict[str, str] = {}
    if dep.condition:
        properties[EDGE_PROP_CONDITION] = dep.condition
    if dep.status:
        properties[EDGE_PROP_STATUS] = dep.status
    return EdgeClass(
        destinationUrn=make_scheduler_datajob_urn(dep.upstream_job_display_name),
        properties=properties or None,
    )


def build_datajob_input_output(
    metadata: SchedulerJobMetadata,
) -> DataJobInputOutputClass:
    input_datajob_edges: list[EdgeClass] = []
    if metadata.writes_job_lineage_edges():
        input_datajob_edges = [
            build_input_datajob_edge(dep) for dep in metadata.job_dependencies
        ]
    return DataJobInputOutputClass(
        inputDatajobs=[],
        inputDatajobEdges=input_datajob_edges,
        inputDatasets=[],
        outputDatasets=[],
    )


class SchedulerDataJobWriter:
    def __init__(
        self,
        gms_url: str | None = None,
        token: str | None = None,
        emitter_factory: EmitterFactory = default_emitter_factory,
        structured_properties_patcher: StructuredPropertiesPatcher | None = None,
    ) -> None:
        self.gms_url = gms_url or os.getenv("DATAHUB_GMS_URL", "http://127.0.0.1:8080")
        self.token = token or os.getenv("DATAHUB_GMS_TOKEN")
        self._emitter_factory = emitter_factory
        self._structured_properties_patcher = (
            structured_properties_patcher or patch_datajob_structured_properties
        )

    def write_job(self, metadata: SchedulerJobMetadata) -> None:
        emitter = self._emitter_factory(self.gms_url, self.token)
        urn = make_scheduler_datajob_urn(metadata.job_display_name)

        owner_name = (metadata.job_owner_name or "").strip()
        if owner_name:
            # Upsert the corpUser entity first so the ownership reference is valid
            emitter.emit_mcp(MetadataChangeProposalWrapper(
                entityUrn=_owner_urn(owner_name),
                aspect=build_corpuser_info(owner_name),
            ))

        aspects = [build_datajob_info(metadata), build_datajob_input_output(metadata)]
        ownership = build_datajob_ownership(metadata)
        if ownership is not None:
            aspects.append(ownership)
        doc = build_datajob_documentation(metadata)
        if doc is not None:
            aspects.append(doc)
        for aspect in aspects:
            emitter.emit_mcp(MetadataChangeProposalWrapper(entityUrn=urn, aspect=aspect))
        structured_props = build_datajob_structured_properties(metadata)
        if structured_props:
            self._structured_properties_patcher(
                self.gms_url,
                urn,
                structured_props,
                self.token,
            )

    def write_job_lineage(self, metadata: SchedulerJobMetadata) -> None:
        emitter = self._emitter_factory(self.gms_url, self.token)
        urn = make_scheduler_datajob_urn(metadata.job_display_name)
        for aspect in (
            build_datajob_info(metadata),
            build_datajob_input_output(metadata),
        ):
            emitter.emit_mcp(MetadataChangeProposalWrapper(entityUrn=urn, aspect=aspect))
