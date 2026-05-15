#!/usr/bin/env python3
"""Pilot: one pdw* schedule job → download GitLab ETL file → parse target tables → PATCH DataHub structured props.

Writes on each target Hive dataset (URN blf-prod-hive.<db>.<table>, PROD):
  - urn:li:structuredProperty:blf.data.warehouse.etl_script  (full file text)
  - urn:li:structuredProperty:blf.data.schedule.schedule_url (https://schedule.corp.bianlifeng.com/job/<job_display_name>)

Requires: trino, network to Trino + GitLab + GMS. GitLab: set BLF_GITLAB_PRIVATE_TOKEN if repo is private.
GMS auth: optional DATAHUB_GMS_TOKEN (omit if no auth).

Example (pilot default job pdw_opc_flag_contact, dry-run):

  python3 xander/python/pilot_pdw_job_etl_script_to_structured_properties.py --dry-run
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterable, Optional

try:
    import trino
except ImportError as exc:  # pragma: no cover
    print("Install: python3 -m pip install trino", file=sys.stderr)
    raise SystemExit(2) from exc

# blf-schedule-job-info mapping (extend as needed)
GITLAB_NAME_TO_PROJECT_PATH: dict[str, str] = {
    "analysis-jobs": "data/analysis-jobs",
    "thrall": "smart-order/thrall",
    "mddf-job": "aladdin/mddf-job",
    "strategy-platform": "aladdin/strategy-job",
    "dayu": "logistics-data/dayu",
    "gis_hammurabi_jobs": "gis/gis-hammurabi-jobs",
    "confidentiality-jobs": "data/confidentiality-data-jobs",
    "data_finance": "data/data_dw_data_finance",
    "data_takeaway": "data/data_dw_data_takeaway",
    "data_drink": "data/data_drink",
    "data-autobox-jobs": "opc/data-autobox-jobs",
    "data_userresearch": "data/data_userresearch",
    "data_tech": "data/data_tech",
    "data_equipment": "data/data_equipment",
    "data_or_jobs": "mlg/data_or_jobs",
    "data_support": "data/data_support",
    "supplychain-jobs": "bach/supplychain-jobs",
    "data_analysis_etc_jobs": "data/analysis-etc-jobs",
}

PROP_ETL_SCRIPT = "urn:li:structuredProperty:blf.data.warehouse.etl_script"
PROP_SCHEDULE_URL = "urn:li:structuredProperty:blf.data.schedule.schedule_url"
GITLAB_API = "https://git.corp.bianlifeng.com/api/v4"
SCHEDULE_URL_TEMPLATE = "https://schedule.corp.bianlifeng.com/job/{job}"


def dataset_structured_properties_url(gms_base: str, dataset_urn: str) -> str:
    encoded = urllib.parse.quote(dataset_urn, safe="")
    return f"{gms_base.rstrip('/')}/openapi/v3/entity/dataset/{encoded}/structuredProperties"


def make_hive_dataset_urn(db: str, table: str, platform_instance: str, env: str) -> str:
    return (
        f"urn:li:dataset:(urn:li:dataPlatform:hive,{platform_instance}.{db}.{table},{env})"
    )


def trino_fetch_shell(job_display_name: str) -> tuple[str, str]:
    host = os.getenv("TRINO_HOST", "10.253.7.167")
    port = int(os.getenv("TRINO_PORT", "8081"))
    user = os.getenv("TRINO_USER", "xuan.zhang")
    catalog = os.getenv("TRINO_CATALOG", "hive")
    schema = os.getenv("TRINO_SCHEMA", "default")
    sql = f"""
SELECT job_display_name,
       try(from_utf8(from_base64(shell_commond))) AS shell_command
FROM default.ods_data_platform_dmp_schedule_job_basic_info
WHERE dt = (SELECT max(dt) FROM default.ods_data_platform_dmp_schedule_job_basic_info)
  AND job_display_name = '{job_display_name.replace("'", "''")}'
""".strip()
    conn = trino.dbapi.connect(
        host=host, port=port, user=user, catalog=catalog, schema=schema
    )
    cur = conn.cursor()
    try:
        cur.execute(sql)
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()
    if not rows:
        raise RuntimeError(f"No DMP row for job_display_name={job_display_name!r}")
    name, shell = rows[0][0], rows[0][1]
    if not shell or not str(shell).strip():
        raise RuntimeError(f"Empty shell_command for {name!r}")
    return str(name), str(shell)


def _shell_lines_for_parse(shell: str) -> list[str]:
    """Drop pure comment lines; keep shebang-style #/home/.../w-run-task.sh as executable line."""
    out: list[str] = []
    for line in shell.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#") and "w-run-task.sh" in s:
            s = s.lstrip("#").strip()
            if s:
                out.append(s)
            continue
        if s.startswith("#"):
            continue
        out.append(s)
    return out


def _last_w_run_task_line(shell: str) -> str:
    lines = [l for l in _shell_lines_for_parse(shell) if "w-run-task.sh" in l]
    if not lines:
        raise RuntimeError("No w-run-task.sh line in shell_command")
    return lines[-1].strip()


def extract_gitlab_name_from_shell(shell: str) -> str:
    """…/<gitlab_name>/bin/w-run-task.sh — return gitlab_name."""
    line = _last_w_run_task_line(shell)
    idx = line.find("/bin/w-run-task.sh")
    if idx < 0:
        raise RuntimeError("No /bin/w-run-task.sh in shell_command (cannot resolve gitlab_name)")
    prefix = line[:idx].rstrip()
    seg = prefix.split("/")[-1]
    if not seg:
        raise RuntimeError("Empty gitlab_name segment before /bin/w-run-task.sh")
    return seg


def extract_job_path_and_type(shell: str) -> tuple[str, str]:
    """Return (job_path, kind) where kind is 'job' or 'python'."""
    line = _last_w_run_task_line(shell)
    m = re.search(r"/bin/w-run-task\.sh\s+(.*)$", line)
    if not m:
        raise RuntimeError("Could not parse w-run-task.sh tail from shell_command")
    tail = m.group(1).strip()
    if tail.lower().startswith("python "):
        rest = tail[7:].strip()
        env_markers = (" prod", " before", " after")
        cut = len(rest)
        for em in env_markers:
            p = rest.find(em)
            if p != -1 and p < cut:
                cut = p
        for tok in rest.split():
            if tok.startswith("--"):
                p = rest.find(tok)
                if p != -1 and p < cut:
                    cut = min(cut, p)
        path = rest[:cut].strip()
        return path, "python"
    rest = tail
    cut = len(rest)
    for tok in rest.split():
        if tok in ("prod", "before", "after") or tok.isdigit() or tok.startswith("--"):
            p = rest.find(tok)
            if p != -1 and p < cut:
                cut = min(cut, p)
    path = rest[:cut].strip()
    return path, "job"


def job_file_name_from_path(job_path: str, kind: str) -> str:
    base = job_path.replace("/", "_")
    return f"{base}.py" if kind == "python" else f"{base}.job"


def first_path_segment(job_path: str) -> str:
    return job_path.split("/")[0]


def resolve_project_path(gitlab_name: str) -> str:
    if gitlab_name not in GITLAB_NAME_TO_PROJECT_PATH:
        raise RuntimeError(
            f"Unknown gitlab_name={gitlab_name!r}; add mapping in GITLAB_NAME_TO_PROJECT_PATH"
        )
    return GITLAB_NAME_TO_PROJECT_PATH[gitlab_name]


def gitlab_project_id(project_path: str, token: Optional[str]) -> int:
    enc = urllib.parse.quote(project_path, safe="")
    url = f"{GITLAB_API}/projects/{enc}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if token:
        req.add_header("PRIVATE-TOKEN", token)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            raise RuntimeError(
                f"GitLab project HTTP {e.code} for {project_path!r}. "
                "Private projects need BLF_GITLAB_PRIVATE_TOKEN (or --gitlab-token)."
            ) from e
        raise
    pid = data.get("id")
    if pid is None:
        raise RuntimeError(f"No project id in GitLab response for {project_path!r}")
    return int(pid)


def gitlab_file_raw(project_id: int, file_path: str, token: Optional[str], ref: str) -> str:
    enc = urllib.parse.quote(file_path, safe="")
    url = f"{GITLAB_API}/projects/{project_id}/repository/files/{enc}?ref={urllib.parse.quote(ref)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if token:
        req.add_header("PRIVATE-TOKEN", token)
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    b64 = data.get("content")
    if not b64:
        raise RuntimeError(f"GitLab file response missing content: {data}")
    return base64.b64decode(b64).decode("utf-8", errors="replace")


def gitlab_file_raw_try_paths(
    project_id: int,
    paths: Iterable[str],
    token: Optional[str],
    ref: str,
) -> tuple[str, str]:
    """Return (used_path, decoded_utf8). Tries each path; refs ref then alternate master/main."""
    last_err: Optional[BaseException] = None
    alt = "main" if ref == "master" else "master"
    for fp in paths:
        for r in (ref, alt):
            try:
                return fp, gitlab_file_raw(project_id, fp, token, r)
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code == 404:
                    continue
                raise
            except Exception as e:
                last_err = e
                raise
    raise RuntimeError(f"GitLab file not found under candidates {list(paths)}: {last_err}")


def gitlab_search_blob_paths(
    project_id: int,
    jfn: str,
    token: Optional[str],
    ref: str,
) -> list[str]:
    """Return blob paths from GitLab project search (needs token for private repos)."""
    if not token:
        return []
    q = urllib.parse.quote(jfn, safe="")
    url = (
        f"{GITLAB_API}/projects/{project_id}/search?scope=blobs&search={q}"
        f"&ref={urllib.parse.quote(ref)}"
    )
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    req.add_header("PRIVATE-TOKEN", token)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError:
        return []
    if not isinstance(data, list):
        return []
    paths: list[str] = []
    for item in data:
        p = item.get("path")
        if isinstance(p, str) and (p.endswith(jfn) or p.endswith("/" + jfn)):
            paths.append(p)
    return list(dict.fromkeys(paths))


def download_gitlab_etl(
    project_path: str,
    job_path: str,
    first_seg: str,
    jfn: str,
    explicit_path: Optional[str],
    token: Optional[str],
    ref: str,
) -> tuple[str, str]:
    """Resolve (path, content) from GitLab using candidates then optional blob search."""
    pid = gitlab_project_id(project_path, token)
    if explicit_path:
        paths = [explicit_path]
    else:
        paths = candidate_gitlab_paths(job_path, first_seg, jfn)
    try:
        return gitlab_file_raw_try_paths(pid, paths, token, ref)
    except RuntimeError as first_err:
        found = gitlab_search_blob_paths(pid, jfn, token, ref)
        if not found:
            raise first_err
        return gitlab_file_raw_try_paths(pid, found, token, ref)


def candidate_gitlab_paths(job_path: str, first_seg: str, jfn: str) -> list[str]:
    """Try common analysis-jobs layouts (skill default + repo variants)."""
    jp = job_path.strip().strip("/")
    return list(
        dict.fromkeys(
            [
                f"jobs/{first_seg}/{jfn}",
                f"jobs/{jp}.job",
                f"jobs/{jp}.py",
                f"jobs/{jp}/{jfn}",
            ]
        )
    )


_INSERT_RE = re.compile(
    r"""
    (?is)
    INSERT\s+(?:INTO|OVERWRITE\s+TABLE)\s+
    (?:IF\s+NOT\s+EXISTS\s+)?
    [`"]?([\w.]+)[`"]?
    """,
    re.VERBOSE,
)

_CREATE_TABLE_RE = re.compile(
    r"""(?is)CREATE\s+(?:EXTERNAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`"]?([\w.]+)[`"]?""",
    re.VERBOSE,
)

# TABLE_NAME="pdw_opc_flag_contact" / TABLE_NAME='x' (common in analysis-jobs .job)
_SHELL_VAR_ASSIGN_RE = re.compile(
    r"""^\s*([A-Z][A-Z0-9_]*)\s*=\s*["']([^"']*)["']\s*$""",
    re.MULTILINE,
)


def expand_job_shell_vars_for_sql(text: str) -> str:
    """Expand $VAR / ${VAR} from ALL_CAPS=\"value\" lines so INSERT INTO $TABLE_NAME matches."""
    subst: dict[str, str] = {}
    for m in _SHELL_VAR_ASSIGN_RE.finditer(text):
        k, v = m.group(1), m.group(2)
        if k and v:
            subst[k] = v
    out = text
    for k in sorted(subst, key=len, reverse=True):
        v = subst[k]
        out = out.replace("${" + k + "}", v)
        out = out.replace("$" + k, v)
    return out


def _normalize_table(fq: str) -> tuple[str, str]:
    fq = fq.strip().strip("`").strip('"')
    if not fq or fq == "*":
        raise ValueError("empty table")
    if "." in fq:
        db, tbl = fq.rsplit(".", 1)
        return db.strip(), tbl.strip()
    return "default", fq


def extract_target_tables(sql_text: str) -> set[tuple[str, str]]:
    """(db, table) from INSERT INTO / INSERT OVERWRITE and CREATE TABLE."""
    found: set[tuple[str, str]] = set()
    for pat in (_INSERT_RE, _CREATE_TABLE_RE):
        for m in pat.finditer(sql_text):
            try:
                found.add(_normalize_table(m.group(1)))
            except ValueError:
                continue
    return found


def patch_two_props(
    gms_url: str,
    dataset_urn: str,
    etl_text: str,
    schedule_url: str,
    token: Optional[str],
) -> None:
    """PATCH structuredProperties; use ``add`` so missing keys on dataset still work."""
    body = {
        "patch": [
            {
                "op": "add",
                "path": f"/properties/{PROP_ETL_SCRIPT}",
                "value": {
                    "propertyUrn": PROP_ETL_SCRIPT,
                    "values": [{"string": etl_text}],
                },
            },
            {
                "op": "add",
                "path": f"/properties/{PROP_SCHEDULE_URL}",
                "value": {
                    "propertyUrn": PROP_SCHEDULE_URL,
                    "values": [{"string": schedule_url}],
                },
            },
        ],
        "arrayPrimaryKeys": {"properties": ["propertyUrn"]},
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        dataset_structured_properties_url(gms_url, dataset_urn),
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
                raise RuntimeError(f"PATCH structuredProperties HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"PATCH failed HTTP {e.code}: {detail}") from e


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--job",
        default="pdw_opc_flag_contact",
        help="job_display_name (pilot default: pdw_opc_flag_contact)",
    )
    p.add_argument(
        "--datahub-gms",
        default=os.getenv("DATAHUB_GMS_URL", "http://127.0.0.1:8080"),
    )
    p.add_argument("--token", default=os.getenv("DATAHUB_GMS_TOKEN"))
    p.add_argument(
        "--gitlab-token",
        default=os.getenv("BLF_GITLAB_PRIVATE_TOKEN"),
        help="PRIVATE-TOKEN for git.corp.bianlifeng.com (optional if repo allows anonymous read)",
    )
    p.add_argument("--gitlab-ref", default="master", help="Git branch / ref for file fetch")
    p.add_argument(
        "--gitlab-file-path",
        default=None,
        help="Exact repo path under project (overrides auto candidates), e.g. jobs/pdw_opc_flag/pdw_opc_flag_contact.job",
    )
    p.add_argument(
        "--etl-file",
        default=None,
        help="Local UTF-8 file: skip GitLab download (for debugging or manual download)",
    )
    p.add_argument("--platform-instance", default="blf-prod-hive")
    p.add_argument("--env", default="PROD")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    job = args.job.strip()
    try:
        name, shell = trino_fetch_shell(job)
    except Exception as e:
        print(f"ABNORMAL_JOB\t{job}\ttrino:{e}", file=sys.stderr)
        return 3

    try:
        gitlab_name = extract_gitlab_name_from_shell(shell)
        job_path, kind = extract_job_path_and_type(shell)
        jfn = job_file_name_from_path(job_path, kind)
        first_seg = first_path_segment(job_path)
        project_path = resolve_project_path(gitlab_name)
    except Exception as e:
        print(f"ABNORMAL_JOB\t{job}\tshell_parse:{e}", file=sys.stderr)
        return 3

    gitlab_file_path = f"jobs/{first_seg}/{jfn}"
    schedule_url = SCHEDULE_URL_TEMPLATE.format(job=name)

    print("job_display_name:", name)
    print("gitlab_name:", gitlab_name, "project:", project_path)
    print("gitlab path hint:", gitlab_file_path)
    print("schedule_url:", schedule_url)

    try:
        if args.etl_file:
            with open(args.etl_file, encoding="utf-8", errors="replace") as fh:
                content = fh.read()
            used_path = args.etl_file
            print("etl source: local file", used_path)
        else:
            used_path, content = download_gitlab_etl(
                project_path,
                job_path,
                first_seg,
                jfn,
                args.gitlab_file_path,
                args.gitlab_token,
                args.gitlab_ref,
            )
            print("etl source: gitlab file", used_path, "ref", args.gitlab_ref)
    except Exception as e:
        print(f"ABNORMAL_JOB\t{job}\tgitlab:{e}", file=sys.stderr)
        return 3

    expanded = expand_job_shell_vars_for_sql(content)
    targets = extract_target_tables(expanded)
    if not targets:
        print(f"ABNORMAL_JOB\t{job}\tno INSERT/CREATE TABLE targets parsed", file=sys.stderr)
        return 3

    print("parsed targets (db, table):", sorted(targets))
    print("etl file chars:", len(content))

    if args.dry_run:
        print("dry-run: skip DataHub PATCH")
        return 0

    for db, tbl in sorted(targets):
        urn = make_hive_dataset_urn(db, tbl, args.platform_instance, args.env)
        print("PATCH", urn)
        try:
            patch_two_props(
                args.datahub_gms, urn, content, schedule_url, args.token
            )
        except Exception as e:
            print(f"ABNORMAL_JOB\t{job}\tdatahub:{urn}:{e}", file=sys.stderr)
            return 4

    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
