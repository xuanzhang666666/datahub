from pathlib import Path

from job_info_sync_datahub.batch_sync import load_jobs_from_file


def test_load_jobs_from_file_skips_comments_and_dedupes(tmp_path: Path) -> None:
    p = tmp_path / "jobs.txt"
    p.write_text(
        "# header\n"
        "job_a\n"
        "\n"
        "job_b\n"
        "job_a\n"
        "  job_c  \n",
        encoding="utf-8",
    )
    assert load_jobs_from_file(str(p)) == ["job_a", "job_b", "job_c"]
