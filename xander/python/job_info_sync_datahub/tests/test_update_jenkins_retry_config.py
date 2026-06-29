from __future__ import annotations

import json
import io
import stat
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

from job_info_sync_datahub import update_jenkins_retry_config as module
from job_info_sync_datahub.update_jenkins_retry_config import (
    FIXED_DELAY_CLASS,
    NAGINATOR_PUBLISHER_XML,
    JenkinsConfigClient,
    add_naginator_publisher,
    has_naginator_publisher,
    job_config_path,
    parse_jobs,
    read_naginator_settings,
    run_batch,
    safe_backup_filename,
    sha256_text,
)

PLAIN_XML = """<?xml version='1.0' encoding='UTF-8'?>
<project>
  <actions/>
  <publishers>
    <hudson.plugins.example.Notifier/>
  </publishers>
</project>"""

EMPTY_PUBLISHERS_XML = """<project><publishers/></project>"""

CONFIGURED_XML = f"""<project>
  <publishers>
    {NAGINATOR_PUBLISHER_XML}
  </publishers>
</project>"""

OTHER_NAGINATOR_XML = """<project>
  <publishers>
    <com.chikli.hudson.plugin.naginator.NaginatorPublisher plugin="naginator@1.17.2">
      <delay class="com.chikli.hudson.plugin.naginator.ProgressiveDelay"/>
      <maxSchedule>9</maxSchedule>
    </com.chikli.hudson.plugin.naginator.NaginatorPublisher>
  </publishers>
</project>"""


class FakeJenkinsClient:
    def __init__(
        self,
        configs: dict[str, str],
        *,
        get_sequences: dict[str, list[str | Exception]] | None = None,
        update_errors: dict[str, Exception] | None = None,
    ) -> None:
        self.configs = dict(configs)
        self.get_sequences = {
            name: list(values) for name, values in (get_sequences or {}).items()
        }
        self.update_errors = update_errors or {}
        self.get_calls: list[str] = []
        self.update_calls: list[tuple[str, str]] = []

    def get_config(self, job_name: str) -> str:
        self.get_calls.append(job_name)
        sequence = self.get_sequences.get(job_name)
        if sequence:
            value = sequence.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        if job_name not in self.configs:
            raise RuntimeError("HTTP 404")
        return self.configs[job_name]

    def update_config(self, job_name: str, config_xml: str) -> None:
        self.update_calls.append((job_name, config_xml))
        if job_name in self.update_errors:
            raise self.update_errors[job_name]
        self.configs[job_name] = config_xml


class _FakeResponse:
    def __init__(self, payload: bytes = b"", status: int = 200) -> None:
        self.payload = payload
        self.status = status

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_parse_jobs_skips_comments_blanks_and_deduplicates() -> None:
    input_count, jobs = parse_jobs(
        "\n# rollout batch\njob_a\n job_b \njob_a\n\n"
    )

    assert input_count == 3
    assert jobs == ["job_a", "job_b"]


def test_job_config_path_supports_folder_jobs() -> None:
    assert job_config_path("folder/child job") == (
        "job/folder/job/child%20job/config.xml"
    )


def test_naginator_detection_accepts_any_direct_publisher_configuration() -> None:
    assert has_naginator_publisher(OTHER_NAGINATOR_XML) is True
    assert read_naginator_settings(OTHER_NAGINATOR_XML) is None


def test_add_naginator_publisher_handles_empty_publishers() -> None:
    updated = add_naginator_publisher(
        EMPTY_PUBLISHERS_XML,
        NAGINATOR_PUBLISHER_XML,
    )

    assert has_naginator_publisher(updated) is True
    assert read_naginator_settings(updated) == (60, 1)


def test_safe_backup_filename_does_not_contain_path_segments() -> None:
    filename = safe_backup_filename("../../folder/job")

    assert "/" not in filename
    assert ".." not in filename
    assert filename.endswith(".xml")


def test_apply_skips_existing_without_backup_or_post(tmp_path: Path) -> None:
    client = FakeJenkinsClient({"configured": OTHER_NAGINATOR_XML})

    exit_code, summary, run_dir = run_batch(
        client,
        action="apply",
        jobs_text="configured",
        backup_root=tmp_path,
        run_id="apply-skip",
    )

    assert exit_code == 0
    assert summary["counts"]["skipped_existing"] == 1
    assert client.update_calls == []
    assert list((run_dir / "xml").glob("*.xml")) == []


def test_apply_backs_up_with_private_permissions_and_verifies(
    tmp_path: Path,
) -> None:
    client = FakeJenkinsClient({"job_a": PLAIN_XML})

    exit_code, summary, run_dir = run_batch(
        client,
        action="apply",
        jobs_text="job_a",
        backup_root=tmp_path,
        run_id="apply-success",
    )

    assert exit_code == 0
    assert summary["counts"]["updated"] == 1
    assert read_naginator_settings(client.configs["job_a"]) == (60, 1)
    backup = next((run_dir / "xml").glob("*.xml"))
    assert backup.read_text(encoding="utf-8") == PLAIN_XML
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(run_dir.stat().st_mode) == 0o700
    manifest = _read_jsonl(run_dir / "manifest.jsonl")
    assert manifest[0]["original_sha256"] == sha256_text(PLAIN_XML)
    assert manifest[0]["updated_sha256"] == sha256_text(
        client.configs["job_a"]
    )


def test_apply_rejects_concurrent_change_before_post(tmp_path: Path) -> None:
    changed_xml = PLAIN_XML.replace("<actions/>", "<actions><x/></actions>")
    client = FakeJenkinsClient(
        {"job_a": changed_xml},
        get_sequences={"job_a": [PLAIN_XML, changed_xml]},
    )

    exit_code, summary, _ = run_batch(
        client,
        action="apply",
        jobs_text="job_a",
        backup_root=tmp_path,
        run_id="apply-concurrent",
    )

    assert exit_code == 1
    assert summary["failure_counts"] == {"failed_concurrent_change": 1}
    assert client.update_calls == []


def test_batch_continues_after_failure_and_prints_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    client = FakeJenkinsClient(
        {"good": PLAIN_XML, "skipped": OTHER_NAGINATOR_XML},
        get_sequences={"missing": [RuntimeError("HTTP 404")]},
    )

    exit_code, summary, _ = run_batch(
        client,
        action="apply",
        jobs_text="missing\ngood\nskipped",
        backup_root=tmp_path,
        run_id="apply-partial",
    )

    output = capsys.readouterr().out
    assert exit_code == 1
    assert summary["counts"] == {
        "updated": 1,
        "skipped_existing": 1,
        "failed": 1,
    }
    assert summary["failure_counts"] == {"failed_get": 1}
    assert "[FAILED_GET] missing" in output
    assert "[UPDATED] good" in output
    assert "[SKIPPED_EXISTING] skipped" in output


@pytest.mark.parametrize(
    ("config_xml", "expected_failure"),
    [
        ("<project>", "failed_invalid_xml"),
        ("<flow-definition/>", "failed_unsupported_job"),
        ("<project><actions/></project>", "failed_unsupported_job"),
    ],
)
def test_apply_classifies_invalid_and_unsupported_jobs(
    tmp_path: Path,
    config_xml: str,
    expected_failure: str,
) -> None:
    client = FakeJenkinsClient({"job_a": config_xml})

    exit_code, summary, _ = run_batch(
        client,
        action="apply",
        jobs_text="job_a",
        backup_root=tmp_path,
        run_id=f"classify-{expected_failure}",
    )

    assert exit_code == 1
    assert summary["failure_counts"] == {expected_failure: 1}


def test_apply_classifies_post_and_verify_failures(tmp_path: Path) -> None:
    post_client = FakeJenkinsClient(
        {"post_fail": PLAIN_XML},
        update_errors={"post_fail": RuntimeError("HTTP 403")},
    )
    verify_client = FakeJenkinsClient(
        {"verify_fail": PLAIN_XML},
        get_sequences={
            "verify_fail": [PLAIN_XML, PLAIN_XML, PLAIN_XML]
        },
    )

    post_code, post_summary, _ = run_batch(
        post_client,
        action="apply",
        jobs_text="post_fail",
        backup_root=tmp_path,
        run_id="post-fail",
    )
    verify_code, verify_summary, _ = run_batch(
        verify_client,
        action="apply",
        jobs_text="verify_fail",
        backup_root=tmp_path,
        run_id="verify-fail",
    )

    assert post_code == 1
    assert post_summary["failure_counts"] == {"failed_post": 1}
    assert verify_code == 1
    assert verify_summary["failure_counts"] == {"failed_verify": 1}


def test_apply_classifies_backup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_write = module._private_write_text

    def fail_xml_backup(path: Path, content: str) -> None:
        if path.suffix == ".xml":
            raise OSError("disk full")
        original_write(path, content)

    monkeypatch.setattr(module, "_private_write_text", fail_xml_backup)

    exit_code, summary, _ = run_batch(
        FakeJenkinsClient({"job_a": PLAIN_XML}),
        action="apply",
        jobs_text="job_a",
        backup_root=tmp_path,
        run_id="backup-fail",
    )

    assert exit_code == 1
    assert summary["failure_counts"] == {"failed_backup": 1}


def test_dry_run_backs_up_but_does_not_post(tmp_path: Path) -> None:
    client = FakeJenkinsClient({"job_a": PLAIN_XML})

    exit_code, summary, run_dir = run_batch(
        client,
        action="dry-run",
        jobs_text="job_a",
        backup_root=tmp_path,
        run_id="dry-run",
    )

    assert exit_code == 0
    assert summary["counts"]["dry_run_ready"] == 1
    assert client.update_calls == []
    assert len(list((run_dir / "xml").glob("*.xml"))) == 1
    assert stat.S_IMODE((run_dir / "summary.json").stat().st_mode) == 0o600


def test_restore_selected_job_requires_current_updated_hash(
    tmp_path: Path,
) -> None:
    client = FakeJenkinsClient({"job_a": PLAIN_XML, "job_b": PLAIN_XML})
    apply_code, _, source_run = run_batch(
        client,
        action="apply",
        jobs_text="job_a\njob_b",
        backup_root=tmp_path,
        run_id="source",
    )
    assert apply_code == 0

    restore_code, summary, _ = run_batch(
        client,
        action="restore",
        jobs_text="job_a",
        backup_root=tmp_path,
        backup_run_dir=source_run,
        run_id="restore-selected",
    )

    assert restore_code == 0
    assert summary["counts"]["restored"] == 1
    assert client.configs["job_a"] == PLAIN_XML
    assert read_naginator_settings(client.configs["job_b"]) == (60, 1)


def test_restore_all_restores_every_successful_update(tmp_path: Path) -> None:
    client = FakeJenkinsClient({"job_a": PLAIN_XML, "job_b": PLAIN_XML})
    _, _, source_run = run_batch(
        client,
        action="apply",
        jobs_text="job_a\njob_b",
        backup_root=tmp_path,
        run_id="source-all",
    )

    exit_code, summary, _ = run_batch(
        client,
        action="restore",
        jobs_text="",
        backup_root=tmp_path,
        backup_run_dir=source_run,
        restore_all=True,
        run_id="restore-all",
    )

    assert exit_code == 0
    assert summary["counts"] == {"restored": 2, "failed": 0}
    assert client.configs == {"job_a": PLAIN_XML, "job_b": PLAIN_XML}


def test_restore_refuses_to_overwrite_later_manual_change(
    tmp_path: Path,
) -> None:
    client = FakeJenkinsClient({"job_a": PLAIN_XML})
    _, _, source_run = run_batch(
        client,
        action="apply",
        jobs_text="job_a",
        backup_root=tmp_path,
        run_id="source",
    )
    client.configs["job_a"] = client.configs["job_a"].replace(
        "<maxSchedule>1</maxSchedule>",
        "<maxSchedule>2</maxSchedule>",
    )

    exit_code, summary, _ = run_batch(
        client,
        action="restore",
        jobs_text="job_a",
        backup_root=tmp_path,
        backup_run_dir=source_run,
        run_id="restore-conflict",
    )

    assert exit_code == 1
    assert summary["failure_counts"] == {"failed_concurrent_change": 1}


def test_restore_all_requires_explicit_flag(tmp_path: Path) -> None:
    source_run = tmp_path / "source"
    source_run.mkdir()

    with pytest.raises(ValueError, match="RESTORE_ALL"):
        run_batch(
            FakeJenkinsClient({}),
            action="restore",
            jobs_text="",
            backup_root=tmp_path,
            backup_run_dir=source_run,
            restore_all=False,
            run_id="restore-all-denied",
        )


def test_restore_rejects_backup_path_outside_source_run(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.xml"
    outside.write_text(PLAIN_XML, encoding="utf-8")
    source_run = tmp_path / "source"
    source_run.mkdir()
    (source_run / "manifest.jsonl").write_text(
        json.dumps(
            {
                "job": "job_a",
                "status": "updated",
                "backup_file": "../outside.xml",
                "original_sha256": sha256_text(PLAIN_XML),
                "updated_sha256": sha256_text(CONFIGURED_XML),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    client = FakeJenkinsClient({"job_a": CONFIGURED_XML})

    exit_code, summary, _ = run_batch(
        client,
        action="restore",
        jobs_text="job_a",
        backup_root=tmp_path,
        backup_run_dir=source_run,
        run_id="restore-path-escape",
    )

    assert exit_code == 1
    assert summary["failure_counts"] == {"failed_backup": 1}
    assert client.update_calls == []


def test_client_fetches_crumb_and_sends_it_on_post() -> None:
    calls: list[urllib.request.Request] = []

    def fake_urlopen(
        request: urllib.request.Request, timeout: int
    ) -> _FakeResponse:
        calls.append(request)
        if request.full_url.endswith("/crumbIssuer/api/json"):
            return _FakeResponse(
                b'{"crumbRequestField":"Jenkins-Crumb","crumb":"abc"}'
            )
        return _FakeResponse()

    client = JenkinsConfigClient("http://jenkins", "user", "token")
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        client.update_config("folder/job", PLAIN_XML)

    assert calls[0].full_url == "http://jenkins/crumbIssuer/api/json"
    assert calls[1].full_url == (
        "http://jenkins/job/folder/job/job/config.xml"
    )
    assert calls[1].headers["Jenkins-crumb"] == "abc"


def test_client_allows_jenkins_without_crumb_issuer() -> None:
    calls: list[urllib.request.Request] = []

    def fake_urlopen(
        request: urllib.request.Request, timeout: int
    ) -> _FakeResponse:
        calls.append(request)
        if request.full_url.endswith("/crumbIssuer/api/json"):
            raise urllib.error.HTTPError(
                request.full_url,
                404,
                "Not Found",
                {},
                io.BytesIO(b""),
            )
        return _FakeResponse()

    client = JenkinsConfigClient("http://jenkins", "user", "token")
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        client.update_config("job_a", PLAIN_XML)

    assert len(calls) == 2
    assert "Jenkins-crumb" not in calls[1].headers
