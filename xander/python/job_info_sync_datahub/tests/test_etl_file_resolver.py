"""etl_file_resolver 双源合并单测。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import patch

from job_info_sync_datahub.etl_file_resolver import (
    content_equal,
    merge_contents,
    read_local_candidate,
    resolve_etl_file,
    search_local_paths_by_filename,
)


class TestContentEqual(unittest.TestCase):
    def test_normalizes_crlf(self) -> None:
        self.assertTrue(content_equal("a\r\nb\n", "a\nb\n"))


class TestMergeContents(unittest.TestCase):
    def test_git_only(self) -> None:
        c, tag, disp = merge_contents("git-body", None, rel_path="jobs/x.job", gitlab_name="thrall")
        self.assertEqual(c, "git-body")
        self.assertEqual(tag, "gitlab")
        self.assertTrue(disp.startswith("gitlab:"))

    def test_local_only(self) -> None:
        c, tag, disp = merge_contents(None, "local-body", rel_path="jobs/x.job", gitlab_name="thrall")
        self.assertEqual(c, "local-body")
        self.assertEqual(tag, "local")
        self.assertTrue(disp.startswith("localfolder:"))

    def test_both_same_prefers_git(self) -> None:
        c, tag, disp = merge_contents(
            "same\n", "same\r\n", rel_path="jobs/x.job", gitlab_name="thrall"
        )
        self.assertEqual(c, "same\n")
        self.assertEqual(tag, "gitlab_preferred_same")
        self.assertTrue(disp.startswith("gitlab:"))

    def test_both_diff_prefers_local(self) -> None:
        c, tag, disp = merge_contents(
            "git-v", "local-v", rel_path="jobs/x.job", gitlab_name="thrall"
        )
        self.assertEqual(c, "local-v")
        self.assertEqual(tag, "local_differs")
        self.assertTrue(disp.startswith("localfolder:"))


class TestResolveEtlFile(unittest.TestCase):
    def test_candidate_git_only(self) -> None:
        rel = "jobs/foo/bar.job"

        def read_git(p: str) -> Optional[str]:
            return "from-git" if p == rel else None

        def read_local(p: str) -> Optional[str]:
            return None

        with patch(
            "job_info_sync_datahub.gitlab_client.get_project_id",
            return_value=1,
        ):
            disp, content, src = resolve_etl_file(
                gitlab_name="thrall",
                project_path="smart-order/thrall",
                candidate_paths=[rel],
                job_file_name="bar.job",
                local_root=None,
                read_gitlab_at_path=read_git,
                read_local_at_path=read_local,
            )
        self.assertEqual(content, "from-git")
        self.assertEqual(disp, f"gitlab:{rel}")
        self.assertEqual(src, "gitlab")

    def test_candidate_local_only(self) -> None:
        rel = "jobs/foo/bar.job"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "thrall" / "jobs" / "foo").mkdir(parents=True)
            (root / "thrall" / rel).write_text("from-local", encoding="utf-8")

            def read_git(p: str) -> Optional[str]:
                return None

            with patch(
                "job_info_sync_datahub.gitlab_client.get_project_id",
                return_value=1,
            ):
                disp, content, src = resolve_etl_file(
                    gitlab_name="thrall",
                    project_path="smart-order/thrall",
                    candidate_paths=[rel],
                    job_file_name="bar.job",
                    local_root=root,
                    read_gitlab_at_path=read_git,
                )
            self.assertEqual(content, "from-local")
            self.assertEqual(disp, f"localfolder:thrall/{rel}")
            self.assertEqual(src, "local")

    def test_empty_project_path_skips_gitlab_uses_local_only(self) -> None:
        """project_path 为空时不得调用 GitLab，仅从 localfolder 读。"""
        rel = "jobs/foo/x.job"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data_shop" / "jobs" / "foo").mkdir(parents=True)
            (root / "data_shop" / rel).write_text("local-only-body", encoding="utf-8")

            def read_git(_p: str) -> Optional[str]:
                raise AssertionError("GitLab must not be queried when project_path is empty")

            disp, content, src = resolve_etl_file(
                gitlab_name="data_shop",
                project_path="",
                candidate_paths=[rel],
                job_file_name="x.job",
                local_root=root,
                read_gitlab_at_path=read_git,
            )
        self.assertEqual(content, "local-only-body")
        self.assertEqual(src, "local")
        self.assertTrue(disp.startswith("localfolder:data_shop/"))

    def test_both_same_uses_git(self) -> None:
        rel = "jobs/foo/bar.job"
        body = "identical\n"

        def read_git(p: str) -> Optional[str]:
            return body if p == rel else None

        def read_local(p: str) -> Optional[str]:
            return body if p == rel else None

        with patch(
            "job_info_sync_datahub.gitlab_client.get_project_id",
            return_value=1,
        ):
            disp, content, src = resolve_etl_file(
                gitlab_name="thrall",
                project_path="smart-order/thrall",
                candidate_paths=[rel],
                job_file_name="bar.job",
                local_root=Path("/unused"),
                read_gitlab_at_path=read_git,
                read_local_at_path=read_local,
            )
        self.assertEqual(content, body)
        self.assertEqual(disp, f"gitlab:{rel}")
        self.assertEqual(src, "gitlab_preferred_same")

    def test_both_diff_uses_local(self) -> None:
        rel = "jobs/foo/bar.job"

        def read_git(p: str) -> Optional[str]:
            return "git" if p == rel else None

        def read_local(p: str) -> Optional[str]:
            return "local" if p == rel else None

        with patch(
            "job_info_sync_datahub.gitlab_client.get_project_id",
            return_value=1,
        ):
            disp, content, src = resolve_etl_file(
                gitlab_name="thrall",
                project_path="smart-order/thrall",
                candidate_paths=[rel],
                job_file_name="bar.job",
                local_root=Path("/unused"),
                read_gitlab_at_path=read_git,
                read_local_at_path=read_local,
            )
        self.assertEqual(content, "local")
        self.assertEqual(disp, f"localfolder:thrall/{rel}")
        self.assertEqual(src, "local_differs")

    def test_neither_raises(self) -> None:
        with patch(
            "job_info_sync_datahub.gitlab_client.get_project_id",
            return_value=1,
        ):
            with patch(
                "job_info_sync_datahub.gitlab_client.search_blob_paths",
                return_value=[],
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    resolve_etl_file(
                        gitlab_name="thrall",
                        project_path="smart-order/thrall",
                        candidate_paths=["jobs/missing.job"],
                        job_file_name="missing.job",
                        local_root=None,
                        read_gitlab_at_path=lambda _p: None,
                        read_local_at_path=lambda _p: None,
                    )
        self.assertIn("均未找到", str(ctx.exception))


class TestLocalHelpers(unittest.TestCase):
    def test_read_local_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rel = "jobs/a/b.job"
            fp = root / "analysis-jobs" / rel
            fp.parent.mkdir(parents=True)
            fp.write_text("hello", encoding="utf-8")
            self.assertEqual(
                read_local_candidate("analysis-jobs", rel, local_root=root),
                "hello",
            )

    def test_search_local_by_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rel = "jobs/pdw/foo.job"
            fp = root / "gis_hammurabi_jobs" / rel
            fp.parent.mkdir(parents=True)
            fp.write_text("x", encoding="utf-8")
            found = search_local_paths_by_filename(
                "gis_hammurabi_jobs", "foo.job", local_root=root
            )
            self.assertEqual(found, [rel])


if __name__ == "__main__":
    unittest.main()
