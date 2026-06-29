"""Batch-add and restore Jenkins Naginator retry configuration safely."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

FIXED_DELAY_CLASS = "com.chikli.hudson.plugin.naginator.FixedDelay"
NAGINATOR_PUBLISHER_TAG = (
    "com.chikli.hudson.plugin.naginator.NaginatorPublisher"
)
NAGINATOR_PUBLISHER_XML = f"""<com.chikli.hudson.plugin.naginator.NaginatorPublisher plugin="naginator@1.17.2">
      <regexpForRerun></regexpForRerun>
      <rerunIfUnstable>false</rerunIfUnstable>
      <rerunMatrixPart>false</rerunMatrixPart>
      <checkRegexp>false</checkRegexp>
      <regexpForMatrixStrategy>TestParent</regexpForMatrixStrategy>
      <delay class="{FIXED_DELAY_CLASS}">
        <delay>60</delay>
      </delay>
      <maxSchedule>1</maxSchedule>
    </com.chikli.hudson.plugin.naginator.NaginatorPublisher>"""

_FAILURE_STATUSES = {
    "failed_get",
    "failed_invalid_xml",
    "failed_unsupported_job",
    "failed_backup",
    "failed_concurrent_change",
    "failed_post",
    "failed_verify",
}


class JenkinsClient(Protocol):
    def get_config(self, job_name: str) -> str: ...

    def update_config(self, job_name: str, config_xml: str) -> None: ...


class JenkinsRequestError(RuntimeError):
    """Jenkins HTTP request failure with a safe, bounded message."""


def parse_jobs(text: str) -> tuple[int, list[str]]:
    """Parse one job per line, skipping blanks/comments and preserving order."""
    candidates: list[str] = []
    for line in (text or "").replace("\r", "").splitlines():
        job = line.strip()
        if not job or job.startswith("#"):
            continue
        candidates.append(job)
    return len(candidates), list(dict.fromkeys(candidates))


def job_config_path(job_name: str) -> str:
    """Build a Jenkins config.xml path for root or folder jobs."""
    segments = [segment.strip() for segment in job_name.split("/")]
    if not segments or any(not segment for segment in segments):
        raise ValueError(f"invalid Jenkins job name: {job_name!r}")
    encoded = "/job/".join(
        urllib.parse.quote(segment, safe="") for segment in segments
    )
    return f"job/{encoded}/config.xml"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def safe_backup_filename(job_name: str) -> str:
    """Return a readable filename that cannot escape the backup directory."""
    readable = re.sub(r"[^A-Za-z0-9._-]+", "_", job_name).strip("._-")
    readable = readable[:100] or "job"
    digest = hashlib.sha256(job_name.encode("utf-8")).hexdigest()[:12]
    return f"{readable}_{digest}.xml"


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_project(config_xml: str) -> ET.Element:
    return ET.fromstring(config_xml)


def _publishers(root: ET.Element) -> ET.Element | None:
    return next(
        (child for child in root if _local_tag(child.tag) == "publishers"),
        None,
    )


def has_naginator_publisher(config_xml: str) -> bool:
    """Check for any direct NaginatorPublisher under publishers."""
    publishers = _publishers(_parse_project(config_xml))
    if publishers is None:
        return False
    return any(
        _local_tag(publisher.tag) == NAGINATOR_PUBLISHER_TAG
        for publisher in publishers
    )


def read_naginator_settings(config_xml: str) -> tuple[int, int] | None:
    """Return FixedDelay settings when the expected fields are readable."""
    publishers = _publishers(_parse_project(config_xml))
    if publishers is None:
        return None
    for publisher in publishers:
        if _local_tag(publisher.tag) != NAGINATOR_PUBLISHER_TAG:
            continue
        delay_parent = next(
            (
                child
                for child in publisher
                if _local_tag(child.tag) == "delay"
                and child.attrib.get("class") == FIXED_DELAY_CLASS
            ),
            None,
        )
        max_schedule = next(
            (
                child
                for child in publisher
                if _local_tag(child.tag) == "maxSchedule"
            ),
            None,
        )
        if delay_parent is None or max_schedule is None:
            return None
        delay = next(
            (
                child
                for child in delay_parent
                if _local_tag(child.tag) == "delay"
            ),
            None,
        )
        if delay is None:
            return None
        try:
            return int(delay.text or ""), int(max_schedule.text or "")
        except ValueError:
            return None
    return None


def add_naginator_publisher(target_xml: str, publisher_xml: str) -> str:
    """Insert the standard publisher while preserving the remaining XML text."""
    root = _parse_project(target_xml)
    if _local_tag(root.tag) != "project":
        raise ValueError(f"unsupported Jenkins job root: {_local_tag(root.tag)}")
    if has_naginator_publisher(target_xml):
        return target_xml
    if _publishers(root) is None:
        raise ValueError("target job has no publishers element")

    empty_publishers = re.compile(r"<publishers(?:\s[^>]*)?\s*/>")
    if empty_publishers.search(target_xml):
        replacement = f"<publishers>\n    {publisher_xml}\n  </publishers>"
        updated = empty_publishers.sub(replacement, target_xml, count=1)
    elif "</publishers>" in target_xml:
        updated = target_xml.replace(
            "</publishers>",
            f"  {publisher_xml}\n  </publishers>",
            1,
        )
    else:
        raise ValueError("target job has no writable publishers element")
    _parse_project(updated)
    return updated


class JenkinsConfigClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        token: str,
        timeout_sec: int = 30,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.token = token
        self.timeout_sec = timeout_sec
        self._crumb: dict[str, str] | None = None

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _auth_headers(self) -> dict[str, str]:
        raw = f"{self.username}:{self.token}".encode("utf-8")
        return {
            "Authorization": "Basic "
            + base64.b64encode(raw).decode("ascii")
        }

    def get_config(self, job_name: str) -> str:
        request = urllib.request.Request(
            self._url(job_config_path(job_name)),
            headers={**self._auth_headers(), "Accept": "application/xml"},
        )
        return self._open(request).decode("utf-8")

    def get_crumb(self) -> dict[str, str]:
        if self._crumb is not None:
            return self._crumb
        request = urllib.request.Request(
            self._url("crumbIssuer/api/json"),
            headers={**self._auth_headers(), "Accept": "application/json"},
        )
        try:
            payload = json.loads(self._open(request).decode("utf-8"))
        except JenkinsRequestError as exc:
            if "HTTP 404" in str(exc):
                self._crumb = {}
                return self._crumb
            raise
        field = payload.get("crumbRequestField")
        value = payload.get("crumb")
        if not isinstance(field, str) or not isinstance(value, str):
            raise JenkinsRequestError("invalid Jenkins crumb response")
        self._crumb = {field: value}
        return self._crumb

    def update_config(self, job_name: str, config_xml: str) -> None:
        request = urllib.request.Request(
            self._url(job_config_path(job_name)),
            data=config_xml.encode("utf-8"),
            method="POST",
            headers={
                **self._auth_headers(),
                **self.get_crumb(),
                "Content-Type": "application/xml; charset=utf-8",
            },
        )
        self._open(request)

    def _open(self, request: urllib.request.Request) -> bytes:
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_sec
            ) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise JenkinsRequestError(
                f"Jenkins HTTP {exc.code}: {detail}"
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise JenkinsRequestError(f"Jenkins request failed: {exc}") from exc


def _private_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(content)
    path.chmod(0o600)


def _append_manifest(path: Path, record: dict[str, Any]) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        0o600,
    )
    with os.fdopen(descriptor, "a", encoding="utf-8") as output:
        output.write(json.dumps(record, ensure_ascii=False) + "\n")
    path.chmod(0o600)


def _create_run_dir(backup_root: Path, run_id: str) -> Path:
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_dir = backup_root / run_id
    run_dir.mkdir(mode=0o700)
    run_dir.chmod(0o700)
    xml_dir = run_dir / "xml"
    xml_dir.mkdir(mode=0o700)
    xml_dir.chmod(0o700)
    return run_dir


def _load_source_manifest(source_run: Path) -> dict[str, dict[str, Any]]:
    path = source_run / "manifest.jsonl"
    if not path.is_file():
        raise ValueError(f"backup manifest not found: {path}")
    records: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        job = record.get("job")
        if isinstance(job, str):
            records[job] = record
    return records


def _failure(
    job: str,
    status: str,
    error: Exception | str,
    **fields: Any,
) -> dict[str, Any]:
    if status not in _FAILURE_STATUSES:
        raise ValueError(f"unknown failure status: {status}")
    return {
        "job": job,
        "status": status,
        "error": str(error)[:500],
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        **fields,
    }


def _process_apply_job(
    client: JenkinsClient,
    job: str,
    run_dir: Path,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    try:
        original_xml = client.get_config(job)
    except Exception as exc:
        return _failure(job, "failed_get", exc)
    original_hash = sha256_text(original_xml)
    try:
        root = _parse_project(original_xml)
    except ET.ParseError as exc:
        return _failure(
            job,
            "failed_invalid_xml",
            exc,
            original_sha256=original_hash,
        )
    if has_naginator_publisher(original_xml):
        return {
            "job": job,
            "status": "skipped_existing",
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "original_sha256": original_hash,
            "backup_file": "",
        }
    if _local_tag(root.tag) != "project" or _publishers(root) is None:
        return _failure(
            job,
            "failed_unsupported_job",
            f"root={_local_tag(root.tag)} publishers={_publishers(root) is not None}",
            original_sha256=original_hash,
        )
    try:
        updated_xml = add_naginator_publisher(
            original_xml, NAGINATOR_PUBLISHER_XML
        )
    except (ET.ParseError, ValueError) as exc:
        return _failure(
            job,
            "failed_invalid_xml",
            exc,
            original_sha256=original_hash,
        )

    backup_relative = Path("xml") / safe_backup_filename(job)
    try:
        _private_write_text(run_dir / backup_relative, original_xml)
    except OSError as exc:
        return _failure(
            job,
            "failed_backup",
            exc,
            original_sha256=original_hash,
        )
    common = {
        "original_sha256": original_hash,
        "backup_file": backup_relative.as_posix(),
    }
    if dry_run:
        return {
            "job": job,
            "status": "dry_run_ready",
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "planned_sha256": sha256_text(updated_xml),
            **common,
        }

    try:
        current_xml = client.get_config(job)
    except Exception as exc:
        return _failure(job, "failed_get", exc, **common)
    if sha256_text(current_xml) != original_hash:
        return _failure(
            job,
            "failed_concurrent_change",
            "config.xml changed after initial read",
            **common,
        )
    try:
        client.update_config(job, updated_xml)
    except Exception as exc:
        return _failure(job, "failed_post", exc, **common)
    try:
        verified_xml = client.get_config(job)
    except Exception as exc:
        return _failure(job, "failed_verify", exc, **common)
    if read_naginator_settings(verified_xml) != (60, 1):
        return _failure(
            job,
            "failed_verify",
            "Naginator delay/maxSchedule verification failed",
            updated_sha256=sha256_text(verified_xml),
            **common,
        )
    return {
        "job": job,
        "status": "updated",
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "updated_sha256": sha256_text(verified_xml),
        **common,
    }


def _process_restore_job(
    client: JenkinsClient,
    job: str,
    source_run: Path,
    source_records: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    source = source_records.get(job)
    if not source or source.get("status") != "updated":
        return _failure(job, "failed_backup", "no successful update backup")
    backup_file = source.get("backup_file")
    original_hash = source.get("original_sha256")
    updated_hash = source.get("updated_sha256")
    if not all(
        isinstance(value, str) and value
        for value in (backup_file, original_hash, updated_hash)
    ):
        return _failure(job, "failed_backup", "incomplete backup manifest")
    backup_path = (source_run / str(backup_file)).resolve()
    try:
        backup_path.relative_to(source_run)
    except ValueError:
        return _failure(
            job,
            "failed_backup",
            "backup file points outside the source run directory",
        )
    try:
        original_xml = backup_path.read_text(encoding="utf-8")
    except OSError as exc:
        return _failure(job, "failed_backup", exc)
    if sha256_text(original_xml) != original_hash:
        return _failure(job, "failed_backup", "backup SHA-256 mismatch")
    try:
        current_xml = client.get_config(job)
    except Exception as exc:
        return _failure(job, "failed_get", exc)
    if sha256_text(current_xml) != updated_hash:
        return _failure(
            job,
            "failed_concurrent_change",
            "current config differs from the recorded updated config",
        )
    try:
        client.update_config(job, original_xml)
    except Exception as exc:
        return _failure(job, "failed_post", exc)
    try:
        verified_xml = client.get_config(job)
    except Exception as exc:
        return _failure(job, "failed_verify", exc)
    if sha256_text(verified_xml) != original_hash:
        return _failure(job, "failed_verify", "restored config SHA-256 mismatch")
    return {
        "job": job,
        "status": "restored",
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "original_sha256": original_hash,
        "restored_sha256": sha256_text(verified_xml),
        "source_backup_file": str(backup_file),
    }


def _print_result(record: dict[str, Any]) -> None:
    status = str(record["status"]).upper()
    suffix = f": {record['error']}" if record.get("error") else ""
    print(f"[{status}] {record['job']}{suffix}", flush=True)


def _build_summary(
    *,
    action: str,
    run_id: str,
    input_count: int,
    jobs: list[str],
    records: list[dict[str, Any]],
    run_dir: Path,
    source_run: Path | None,
) -> dict[str, Any]:
    status_counts = Counter(str(record["status"]) for record in records)
    if action == "restore":
        counts = {
            "restored": status_counts.get("restored", 0),
            "failed": sum(
                count
                for status, count in status_counts.items()
                if status in _FAILURE_STATUSES
            ),
        }
    else:
        success_status = "dry_run_ready" if action == "dry-run" else "updated"
        counts = {
            success_status: status_counts.get(success_status, 0),
            "skipped_existing": status_counts.get("skipped_existing", 0),
            "failed": sum(
                count
                for status, count in status_counts.items()
                if status in _FAILURE_STATUSES
            ),
        }
    failure_counts = {
        status: count
        for status, count in sorted(status_counts.items())
        if status in _FAILURE_STATUSES
    }
    jobs_by_status = {
        status: [
            str(record["job"])
            for record in records
            if record["status"] == status
        ]
        for status in sorted(status_counts)
    }
    jobs_by_status["failed"] = [
        str(record["job"])
        for record in records
        if record["status"] in _FAILURE_STATUSES
    ]
    return {
        "action": action,
        "run_id": run_id,
        "input_job_count": input_count,
        "unique_job_count": len(jobs),
        "counts": counts,
        "failure_counts": failure_counts,
        "jobs": jobs_by_status,
        "run_dir": str(run_dir),
        "manifest": str(run_dir / "manifest.jsonl"),
        "source_backup_run": str(source_run) if source_run else "",
    }


def _print_summary(summary: dict[str, Any]) -> None:
    print("\n" + "=" * 68)
    print(
        f"action={summary['action']} "
        f"input={summary['input_job_count']} "
        f"unique={summary['unique_job_count']}"
    )
    for status, count in summary["counts"].items():
        print(f"{status}={count}")
        for job in summary["jobs"].get(status, []):
            print(f"  {job}")
    if summary["failure_counts"]:
        print("failure_counts:")
        for status, count in summary["failure_counts"].items():
            print(f"  {status}={count}")
    print(f"run_dir={summary['run_dir']}")
    print(f"manifest={summary['manifest']}")
    print(f"summary={Path(summary['run_dir']) / 'summary.json'}")
    print("=" * 68)


def run_batch(
    client: JenkinsClient,
    *,
    action: str,
    jobs_text: str,
    backup_root: Path,
    backup_run_dir: Path | None = None,
    restore_all: bool = False,
    run_id: str | None = None,
) -> tuple[int, dict[str, Any], Path]:
    """Run one serial apply, dry-run, or restore batch."""
    if action not in {"apply", "dry-run", "restore"}:
        raise ValueError("ACTION must be apply, dry-run, or restore")
    input_count, jobs = parse_jobs(jobs_text)
    source_records: dict[str, dict[str, Any]] = {}
    source_run: Path | None = None
    if action == "restore":
        if backup_run_dir is None:
            raise ValueError("BACKUP_RUN_DIR is required for restore")
        if not jobs and not restore_all:
            raise ValueError(
                "set RESTORE_ALL=1 to restore the whole backup run"
            )
        source_run = backup_run_dir.resolve()
        source_records = _load_source_manifest(source_run)
        if not jobs:
            jobs = [
                job
                for job, record in source_records.items()
                if record.get("status") == "updated"
            ]
            input_count = len(jobs)
    if not jobs:
        raise ValueError("JOBS contains no valid job names")

    effective_run_id = run_id or (
        f"{action}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
    )
    run_dir = _create_run_dir(backup_root, effective_run_id)
    _private_write_text(run_dir / "jobs_snapshot.txt", "\n".join(jobs) + "\n")
    manifest_path = run_dir / "manifest.jsonl"
    records: list[dict[str, Any]] = []
    for job in jobs:
        if action == "restore":
            assert source_run is not None
            record = _process_restore_job(
                client, job, source_run, source_records
            )
        else:
            record = _process_apply_job(
                client,
                job,
                run_dir,
                dry_run=action == "dry-run",
            )
        records.append(record)
        _append_manifest(manifest_path, record)
        _print_result(record)

    summary = _build_summary(
        action=action,
        run_id=effective_run_id,
        input_count=input_count,
        jobs=jobs,
        records=records,
        run_dir=run_dir,
        source_run=source_run,
    )
    _private_write_text(
        run_dir / "summary.json",
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
    )
    _print_summary(summary)
    return (1 if summary["counts"]["failed"] else 0), summary, run_dir


def _env_bool(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--action",
        choices=("apply", "dry-run", "restore"),
        default=os.getenv("ACTION", "dry-run"),
    )
    parser.add_argument(
        "--jobs",
        default=os.getenv("JOBS") or os.getenv("jobs") or "",
    )
    parser.add_argument(
        "--backup-root",
        type=Path,
        default=Path(
            os.getenv(
                "JENKINS_RETRY_BACKUP_ROOT",
                "/data/datahub/backups/jenkins-retry",
            )
        ),
    )
    parser.add_argument(
        "--backup-run-dir",
        type=Path,
        default=(
            Path(os.environ["BACKUP_RUN_DIR"])
            if os.getenv("BACKUP_RUN_DIR")
            else None
        ),
    )
    parser.add_argument(
        "--restore-all",
        action="store_true",
        default=_env_bool("RESTORE_ALL"),
    )
    args = parser.parse_args(argv)

    username = os.getenv("BLF_JENKINS_USER", "")
    token = os.getenv("BLF_JENKINS_TOKEN") or os.getenv(
        "BLF_JENKINS_PASSWORD", ""
    )
    if not username or not token:
        print(
            "BLF_JENKINS_USER and token/password are required",
            file=sys.stderr,
        )
        return 2
    client = JenkinsConfigClient(
        os.getenv(
            "BLF_JENKINS_URL", "http://schedule.corp.bianlifeng.com"
        ),
        username,
        token,
        timeout_sec=int(os.getenv("BLF_JENKINS_TIMEOUT_SEC", "30")),
    )
    try:
        exit_code, _, _ = run_batch(
            client,
            action=args.action,
            jobs_text=args.jobs,
            backup_root=args.backup_root,
            backup_run_dir=args.backup_run_dir,
            restore_all=args.restore_all,
        )
        return exit_code
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
