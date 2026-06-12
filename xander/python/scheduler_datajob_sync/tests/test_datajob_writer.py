import json
from datetime import datetime

from datahub.metadata.schema_classes import DataJobInfoClass, DataJobInputOutputClass

from scheduler_datajob_sync.datajob_writer import (
    EDGE_PROP_CONDITION,
    EDGE_PROP_STATUS,
    URN_JOB_CONTENT_XML,
    URN_JOB_EXECUTE_SHELL,
    SchedulerDataJobWriter,
    build_datajob_info,
    build_datajob_input_output,
    build_datajob_structured_properties,
    make_scheduler_datajob_urn,
    serialize_job_dependencies,
)
from scheduler_datajob_sync.models import SchedulerJobDependency, SchedulerJobMetadata
from scheduler_datajob_sync.trigger_parser import TRIGGER_JOB_DEPENDENCY, TRIGGER_TIMER


class RecordingEmitter:
    def __init__(self) -> None:
        self.mcps: list[object] = []

    def emit_mcp(self, mcp: object) -> None:
        self.mcps.append(mcp)


def _metadata() -> SchedulerJobMetadata:
    return SchedulerJobMetadata(
        id=123,
        job_name="pdw_example_job",
        job_display_name="PDW_Example_Job",
        job_owner_name="alice",
        job_proxy_user="warehouse",
        line_business_code="pdw",
        last_build_start_time=datetime(2026, 6, 10, 8, 30, 0),
        contacts_name="alice,bob",
        assigned_node="node-a",
        job_disable="false",
        job_priority="5",
        shell_command="echo run",
        content="<job><name>demo</name></job>",
        upstream_jobs=["Upstream_A", "Upstream_B"],
        trigger_types=[TRIGGER_JOB_DEPENDENCY],
        job_dependencies=[
            SchedulerJobDependency("Upstream_A", "d=@$"),
            SchedulerJobDependency("Upstream_B", "h=1"),
        ],
        sms_notify="true",
        im_notify="true",
    )


def test_make_scheduler_datajob_urn_uses_fixed_technical_flow() -> None:
    assert make_scheduler_datajob_urn("PDW_Example_Job") == (
        "urn:li:dataJob:(urn:li:dataFlow:(blf-schedule,blf-schedule,PROD),"
        "PDW_Example_Job)"
    )


def test_build_datajob_info_maps_metadata_to_custom_properties() -> None:
    info = build_datajob_info(_metadata())

    assert isinstance(info, DataJobInfoClass)
    assert info.name == "PDW_Example_Job"
    assert info.type == "BLF_SCHEDULE_JOB"
    assert "负责人: alice" in (info.description or "")
    assert info.customProperties["job_id"] == "123"
    assert info.customProperties["job_owner_name"] == "alice"
    assert info.customProperties["job_proxy_user"] == "warehouse"
    assert info.customProperties["line_business_code"] == "pdw"
    assert info.customProperties["job_disable"] == "false"
    assert info.customProperties["last_build_start_time"] == "2026-06-10T08:30:00"
    assert info.customProperties["schedule_url"].endswith("/job/PDW_Example_Job")
    assert info.customProperties["trigger_type"] == TRIGGER_JOB_DEPENDENCY
    assert json.loads(info.customProperties["scheduler_job_dependencies"]) == [
        {"upstream": "Upstream_A", "condition": "d=@$", "status": "SUCCESS"},
        {"upstream": "Upstream_B", "condition": "h=1", "status": "SUCCESS"},
    ]
    assert "shell_command" not in info.customProperties


def test_build_datajob_input_output_maps_upstream_jobs_to_input_edges() -> None:
    aspect = build_datajob_input_output(_metadata())

    assert isinstance(aspect, DataJobInputOutputClass)
    assert aspect.inputDatajobs == []
    assert len(aspect.inputDatajobEdges) == 2
    assert aspect.inputDatajobEdges[0].destinationUrn == make_scheduler_datajob_urn(
        "Upstream_A"
    )
    assert aspect.inputDatajobEdges[0].properties[EDGE_PROP_CONDITION] == "d=@$"
    assert aspect.inputDatajobEdges[0].properties[EDGE_PROP_STATUS] == "SUCCESS"
    assert aspect.inputDatajobEdges[1].destinationUrn == make_scheduler_datajob_urn(
        "Upstream_B"
    )
    assert aspect.inputDatajobEdges[1].properties[EDGE_PROP_CONDITION] == "h=1"


def test_build_datajob_input_output_skips_edges_for_timer_jobs() -> None:
    metadata = SchedulerJobMetadata(
        job_display_name="Timer_Job",
        trigger_types=[TRIGGER_TIMER],
        cron_schedule="05 13 * * *",
        upstream_jobs=["stale_upstream"],
        job_dependencies=[SchedulerJobDependency("stale_upstream", "d=@$")],
    )

    aspect = build_datajob_input_output(metadata)

    assert aspect.inputDatajobs == []
    assert aspect.inputDatajobEdges == []


def test_serialize_job_dependencies_preserves_case_sensitive_names() -> None:
    payload = serialize_job_dependencies(
        [SchedulerJobDependency("PDW_Example_Job", "d=@$")]
    )

    assert '"PDW_Example_Job"' in payload


def test_build_datajob_structured_properties_maps_shell_and_xml() -> None:
    props = build_datajob_structured_properties(_metadata())
    by_urn = {prop.property_urn: prop.string_value for prop in props}

    assert URN_JOB_EXECUTE_SHELL in by_urn
    assert URN_JOB_CONTENT_XML in by_urn
    assert "echo run" in by_urn[URN_JOB_EXECUTE_SHELL]
    assert "<job><name>demo</name></job>" in by_urn[URN_JOB_CONTENT_XML]
    assert by_urn[URN_JOB_EXECUTE_SHELL].startswith("```shell")
    assert by_urn[URN_JOB_CONTENT_XML].startswith("```xml")


def test_writer_emits_info_and_input_output_aspects() -> None:
    emitter = RecordingEmitter()
    patched: list[tuple[str, list[object]]] = []

    def _patch(gms_url: str, urn: str, props: list[object], token: str | None) -> None:
        patched.append((urn, props))

    writer = SchedulerDataJobWriter(
        emitter_factory=lambda _url, _token: emitter,
        structured_properties_patcher=_patch,
    )

    writer.write_job(_metadata())

    assert len(emitter.mcps) == 2
    assert len(patched) == 1
    assert patched[0][0] == make_scheduler_datajob_urn("PDW_Example_Job")
    assert len(patched[0][1]) == 2
    assert [mcp.entityUrn for mcp in emitter.mcps] == [
        make_scheduler_datajob_urn("PDW_Example_Job"),
        make_scheduler_datajob_urn("PDW_Example_Job"),
    ]
    assert isinstance(emitter.mcps[0].aspect, DataJobInfoClass)
    assert isinstance(emitter.mcps[1].aspect, DataJobInputOutputClass)


def test_writer_lineage_only_emits_info_and_input_output_without_structured_props() -> None:
    emitter = RecordingEmitter()
    patched: list[tuple[str, list[object]]] = []

    def _patch(gms_url: str, urn: str, props: list[object], token: str | None) -> None:
        patched.append((urn, props))

    writer = SchedulerDataJobWriter(
        emitter_factory=lambda _url, _token: emitter,
        structured_properties_patcher=_patch,
    )

    writer.write_job_lineage(_metadata())

    assert len(emitter.mcps) == 2
    assert patched == []
