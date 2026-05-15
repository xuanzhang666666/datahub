"""单作业血缘调试 — 与 batch_sync / sync_job_lineage 同路径，输出可机器读报告。

供 CLI ``scripts/debug_job_lineage.py`` 或其它工具调用。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple

from .datahub_writer import make_dataset_urn_from_ref
from .gitlab_client import download_etl_file
from .hive_fqtn_validation import filter_table_lineages_by_hive_fqtn_rules, is_valid_hive_fqtn
from .hive_table_existence import (
    filter_lineages_require_full_hive_presence,
    should_skip_hive_existence_check,
)
from .lineage_llm_compare import call_llm_extract, load_env_file
from .lineage_write_policy import (
    LineageWriteDecision,
    _parse_lineage_array,
    apply_lineage_filters_from_parsed,
    tables_from_llm_payload,
)
from .models import JobMetadata, TableLineage
from .runtime_parser import (
    candidate_gitlab_paths,
    extract_gitlab_name,
    extract_job_path_and_type,
    get_job_dir_name,
    has_real_w_run_task,
    job_file_name,
    resolve_project_path,
)
from .schedule_client import fetch_job_metadata

OutputFormat = Literal["human", "json", "summary"]


@dataclass
class DebugLineageConfig:
    """调试运行参数（与生产 batch 对齐的项尽量同名）。"""

    etl_file: Optional[str] = None
    etl_content: Optional[str] = None
    gitlab_token: Optional[str] = None
    gitlab_ref: str = "master"
    gitlab_file_path: Optional[str] = None
    llm_timeout_sec: int = 120
    skip_llm: bool = False
    skip_hive_check: bool = False
    llm_raw_file: Optional[str] = None
    platform_instance: str = "blf-prod-hive"
    env: str = "PROD"
    etl_preview_chars: int = 4000
    output_dir: Optional[str] = None
    output_format: OutputFormat = "human"


@dataclass
class DebugLineageReport:
    """单次调试的结构化结果（可 ``to_dict()`` / ``write_json()``）。"""

    job_display_name: str
    ok: bool
    exit_code: int
    error: Optional[str] = None
    schedule_url: str = ""
    metadata: Optional[Dict[str, Any]] = None
    runtime: Optional[Dict[str, Any]] = None
    etl: Optional[Dict[str, Any]] = None
    llm_raw: Optional[Dict[str, Any]] = None
    llm_flat_legacy: Optional[Dict[str, Any]] = None
    parsed_before_filter: List[Dict[str, Any]] = field(default_factory=list)
    fqtn_audit: List[Dict[str, Any]] = field(default_factory=list)
    fqtn_validation: Optional[Dict[str, Any]] = None
    lineages_after_fqtn: List[Dict[str, Any]] = field(default_factory=list)
    hive_existence: Optional[Dict[str, Any]] = None
    lineages_after_hive: List[Dict[str, Any]] = field(default_factory=list)
    decision: Optional[Dict[str, Any]] = None
    final_lineages: List[Dict[str, Any]] = field(default_factory=list)
    dataset_urns: List[Dict[str, str]] = field(default_factory=list)
    dmp_upstream_jobs: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def write_json(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return path


def load_lineage_env_files(extra: Optional[Sequence[str]] = None) -> List[str]:
    """加载常见位置的 lineage .env，返回实际加载的文件路径列表。"""
    candidates: List[Path] = []
    if extra:
        candidates.extend(Path(p) for p in extra)
    env_file = os.environ.get("BLF_LINEAGE_ENV_FILE", "").strip()
    if env_file:
        candidates.append(Path(env_file))
    pkg_root = Path(__file__).resolve().parent.parent
    candidates.extend(
        [
            pkg_root / "scripts" / "lineage.env",
            pkg_root / "lineage.env",
            Path.home() / ".datahub" / "lineage.env",
            Path.home() / "lineage.env",
        ]
    )
    loaded: List[str] = []
    seen: set[str] = set()
    for p in candidates:
        key = str(p.resolve()) if p.is_file() else str(p)
        if key in seen or not p.is_file():
            continue
        seen.add(key)
        load_env_file(p)
        loaded.append(str(p))
    return loaded


def _lineage_to_dict(lineages: List[TableLineage]) -> List[Dict[str, Any]]:
    return [
        {
            "target": tl.target.full_name,
            "upstreams": [u.full_name for u in tl.upstreams],
        }
        for tl in lineages
    ]


def _audit_fqtn_per_table(lineages: List[TableLineage]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for tl in lineages:
        for ref in [tl.target, *tl.upstreams]:
            fn = ref.full_name.lower()
            if fn in seen:
                continue
            seen.add(fn)
            ok, code = is_valid_hive_fqtn(fn)
            rows.append({"fqtn": fn, "valid": ok, "reason": code or "ok"})
    return rows


def _urn_plan(
    lineages: List[TableLineage],
    *,
    platform_instance: str,
    env: str,
) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for tl in lineages:
        out.append(
            {
                "role": "target",
                "fqtn": tl.target.full_name,
                "urn": make_dataset_urn_from_ref(tl.target, platform_instance, env),
            }
        )
        for u in tl.upstreams:
            out.append(
                {
                    "role": "upstream",
                    "fqtn": u.full_name,
                    "target_of": tl.target.full_name,
                    "urn": make_dataset_urn_from_ref(u, platform_instance, env),
                }
            )
    return out


def _fetch_etl(
    metadata: JobMetadata,
    runtime: Dict[str, Any],
    cfg: DebugLineageConfig,
) -> Tuple[str, str, str]:
    """返回 (content, used_path, source_tag)。"""
    if cfg.etl_content is not None:
        return cfg.etl_content, "<inline:etl_content>", "param"
    if cfg.etl_file:
        content = Path(cfg.etl_file).read_text(encoding="utf-8", errors="replace")
        return content, cfg.etl_file, "local_file"
    shell = metadata.shell_command
    if runtime["is_inline_shell"]:
        return shell, "<inline:shell_command>", "inline"
    used_path, content, source = download_etl_file(
        gitlab_name=runtime["gitlab_name"],
        project_path=runtime["project_path"],
        candidate_paths=runtime["gitlab_candidate_paths"],
        job_file_name=runtime["job_file_name"],
        ref=cfg.gitlab_ref,
        explicit_path=cfg.gitlab_file_path,
        token=cfg.gitlab_token,
    )
    return content, used_path, source


def debug_job_lineage(job_display_name: str, cfg: Optional[DebugLineageConfig] = None) -> DebugLineageReport:
    """跑通单作业调试流水线，返回结构化报告（不写入 DataHub）。"""
    cfg = cfg or DebugLineageConfig()
    if cfg.skip_hive_check:
        os.environ["BLF_LINEAGE_SKIP_HIVE_EXISTENCE_CHECK"] = "1"

    report = DebugLineageReport(
        job_display_name=job_display_name,
        ok=False,
        exit_code=0,
        schedule_url=f"https://schedule.corp.bianlifeng.com/job/{job_display_name}",
    )

    try:
        metadata = fetch_job_metadata(job_display_name)
        report.metadata = {
            "job_display_name": metadata.job_display_name,
            "job_name": metadata.job_name,
            "dt": metadata.dt,
            "upstream_jobs": list(metadata.upstream_jobs),
            "shell_command": metadata.shell_command,
        }
        report.dmp_upstream_jobs = list(metadata.upstream_jobs)

        shell = metadata.shell_command
        is_inline = not has_real_w_run_task(shell)
        gitlab_name = extract_gitlab_name(shell)
        project_path = resolve_project_path(gitlab_name)
        job_path, kind = extract_job_path_and_type(shell)
        jfn = job_file_name(job_path, kind)
        job_dir = get_job_dir_name(shell)
        candidates = candidate_gitlab_paths(job_path, jfn, job_dir)
        runtime = {
            "is_inline_shell": is_inline,
            "gitlab_name": gitlab_name,
            "project_path": project_path,
            "job_path": job_path,
            "job_type": kind,
            "job_file_name": jfn,
            "job_dir": job_dir,
            "gitlab_candidate_paths": candidates,
        }
        report.runtime = runtime

        etl_content, used_path, etl_source = _fetch_etl(metadata, runtime, cfg)
        preview = etl_content
        if cfg.etl_preview_chars > 0 and len(preview) > cfg.etl_preview_chars:
            preview = preview[: cfg.etl_preview_chars] + f"\n... [truncated, total {len(etl_content)} chars]"
        report.etl = {
            "path": used_path,
            "source": etl_source,
            "chars": len(etl_content),
            "preview": preview,
        }

        if cfg.skip_llm:
            report.ok = True
            report.exit_code = 0
            return report

        if cfg.llm_raw_file:
            loaded = json.loads(Path(cfg.llm_raw_file).read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and "lineage" in loaded:
                raw = loaded
            elif isinstance(loaded, dict) and isinstance(loaded.get("llm_raw"), dict):
                raw = loaded["llm_raw"]
            else:
                raise ValueError(
                    "llm_raw_file 须为 LLM 响应 JSON（含 lineage），"
                    "或 debug_report.json / 旧版 debug_bundle.json"
                )
        else:
            raw = call_llm_extract(
                etl_content,
                timeout_sec=cfg.llm_timeout_sec,
                job_file_name=jfn,
            )
        report.llm_raw = raw

        flat_t, flat_u = tables_from_llm_payload(raw)
        report.llm_flat_legacy = {
            "target_tables": sorted(flat_t),
            "upstream_tables": sorted(flat_u),
        }

        parsed = _parse_lineage_array(raw)
        report.parsed_before_filter = _lineage_to_dict(parsed)
        report.fqtn_audit = _audit_fqtn_per_table(parsed)

        after_fqtn, fqtn_meta = filter_table_lineages_by_hive_fqtn_rules(parsed)
        report.fqtn_validation = fqtn_meta
        report.lineages_after_fqtn = _lineage_to_dict(after_fqtn)

        if should_skip_hive_existence_check():
            after_hive = after_fqtn
            hive_meta: Dict[str, Any] = {
                "skipped": True,
                "reason": "BLF_LINEAGE_SKIP_HIVE_EXISTENCE_CHECK",
            }
        else:
            after_hive, hive_meta = filter_lineages_require_full_hive_presence(after_fqtn)
        report.hive_existence = hive_meta
        report.lineages_after_hive = _lineage_to_dict(after_hive)

        table_lineages, decision = apply_lineage_filters_from_parsed(parsed, raw)
        report.decision = decision.to_audit_dict()
        report.final_lineages = _lineage_to_dict(table_lineages)
        report.dataset_urns = _urn_plan(
            table_lineages,
            platform_instance=cfg.platform_instance,
            env=cfg.env,
        )

        if not table_lineages:
            report.ok = False
            report.exit_code = 3
        else:
            report.ok = decision.write_upstream_lineage
            report.exit_code = 0 if decision.write_upstream_lineage else 3

        if cfg.output_dir:
            out = Path(cfg.output_dir) / job_display_name
            report.write_json(out / "debug_report.json")

        return report

    except Exception as exc:
        report.ok = False
        report.exit_code = 1
        report.error = str(exc)
        if cfg.output_dir:
            out = Path(cfg.output_dir) / job_display_name
            report.write_json(out / "debug_report.json")
        return report


def _print_human(report: DebugLineageReport) -> None:
    def banner(title: str) -> None:
        print()
        print("=" * 72)
        print(title)
        print("=" * 72)

    def dump(label: str, obj: Any) -> None:
        print(f"\n--- {label} ---")
        if isinstance(obj, (dict, list)):
            print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
        else:
            print(obj)

    banner(f"作业: {report.job_display_name}")
    print(f"schedule: {report.schedule_url}")
    if report.error:
        dump("error", report.error)
        return

    banner("1) DMP 调度元数据")
    dump("metadata", report.metadata)

    banner("2) shell 解析 + ETL")
    dump("runtime", report.runtime)
    dump("etl", {k: v for k, v in (report.etl or {}).items() if k != "preview"})
    if report.etl and report.etl.get("preview"):
        dump("etl_preview", report.etl["preview"])

    if report.llm_raw is None:
        print("\n[skip_llm] 未执行 LLM 阶段")
        return

    banner("3) LLM 原始 JSON")
    dump("llm_raw", report.llm_raw)
    dump("llm_flat_legacy", report.llm_flat_legacy)

    banner("4) 解析后（未过滤）")
    dump("parsed_lineages", report.parsed_before_filter)
    dump("fqtn_audit", report.fqtn_audit)

    banner("5) fqtn 规则")
    dump("fqtn_validation", report.fqtn_validation)
    dump("lineages_after_fqtn", report.lineages_after_fqtn)

    banner("6) Hive 存在性")
    dump("hive_existence", report.hive_existence)
    dump("lineages_after_hive", report.lineages_after_hive)

    banner("7) 最终决策 + DataHub URN")
    dump("decision", report.decision)
    dump("final_lineages", report.final_lineages)
    dump("dataset_urns", report.dataset_urns)
    dump(
        "dmp_upstream_jobs_vs_llm_tables",
        {
            "dmp_upstream_jobs": report.dmp_upstream_jobs,
            "note": "DMP 登记为调度作业名；LLM 上游为 Hive 表 fqtn，二者通常不一一对应",
        },
    )

    status = (report.decision or {}).get("lineage_status", "-")
    write = (report.decision or {}).get("write_upstream_lineage", False)
    print(f"\n结论: status={status} write_upstream_lineage={write} exit={report.exit_code}")


def _print_summary(report: DebugLineageReport) -> None:
    d = report.decision or {}
    print(
        json.dumps(
            {
                "job": report.job_display_name,
                "ok": report.ok,
                "exit_code": report.exit_code,
                "error": report.error,
                "status": d.get("lineage_status"),
                "write": d.get("write_upstream_lineage"),
                "targets": d.get("targets_chosen"),
                "upstreams": d.get("sources_chosen"),
            },
            ensure_ascii=False,
        )
    )


def print_debug_report(report: DebugLineageReport, fmt: OutputFormat = "human") -> None:
    if fmt == "json":
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, default=str))
    elif fmt == "summary":
        _print_summary(report)
    else:
        _print_human(report)


def resolve_job_names(
    jobs: Sequence[str],
    job_file: Optional[str],
) -> List[str]:
    names: List[str] = list(jobs)
    if job_file:
        for line in Path(job_file).read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                names.append(s)
    if not names:
        raise ValueError("至少指定一个作业：--job 或 --job-file")
    return list(dict.fromkeys(names))


def debug_jobs(
    job_names: Sequence[str],
    cfg: DebugLineageConfig,
) -> Tuple[int, List[DebugLineageReport]]:
    """批量调试，返回 (最大 exit_code, 报告列表)。"""
    reports: List[DebugLineageReport] = []
    code = 0
    for name in job_names:
        rep = debug_job_lineage(name, cfg)
        print_debug_report(rep, cfg.output_format)
        reports.append(rep)
        code = max(code, rep.exit_code)
    return code, reports
