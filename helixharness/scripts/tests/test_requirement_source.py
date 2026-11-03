#!/usr/bin/env python3
"""Requirement source resolution: classification, dws planning and ingestion."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import requirement_source as rs


DOC_URL = "https://alidocs.dingtalk.com/i/nodes/QG53mjyd80RNO2DoHe70abNBV6zbX04v"


def _payload(**overrides):
    base = {
        "success": True,
        "title": "示例需求",
        "nodeId": "QG53mjyd80RNO2DoHe70abNBV6zbX04v",
        "docUrl": DOC_URL + "?utm_scene=team_space",
        "markdown": "# 需求\n\n为会员增加猫耳朵标识。\n",
    }
    base.update(overrides)
    return base


def _info(**overrides):
    base = {"success": True, "name": "示例需求", "nodeId": "QG53mjyd80RNO2DoHe70abNBV6zbX04v", "extension": "adoc"}
    base.update(overrides)
    return base


def _comments(**overrides):
    base = {"success": True, "commentList": [], "hasMore": False}
    base.update(overrides)
    return base


class ClassifyTests(unittest.TestCase):
    def test_dingtalk_url_is_recognised_and_canonicalised(self):
        result = rs.classify_source(DOC_URL + "?utm_scene=team_space#section")
        self.assertEqual("dingtalk_doc", result["kind"])
        self.assertEqual("QG53mjyd80RNO2DoHe70abNBV6zbX04v", result["node_id"])
        self.assertEqual(DOC_URL, result["normalized"])

    def test_plain_text_stays_text(self):
        self.assertEqual("text", rs.classify_source("给任务列表增加状态筛选")["kind"])

    def test_existing_file_is_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "requirement.md"
            path.write_text("# 需求\n", encoding="utf-8")
            result = rs.classify_source(str(path))
            self.assertEqual("file", result["kind"])
            self.assertEqual(str(path.resolve()), result["normalized"])

    def test_missing_path_is_blocked(self):
        with self.assertRaises(rs.SourceError) as caught:
            rs.classify_source("requirements/missing.md")
        self.assertEqual("requirement_source_path_missing", caught.exception.reason)

    def test_other_url_is_passed_through(self):
        result = rs.classify_source("https://example.com/spec?a=1")
        self.assertEqual("url", result["kind"])
        self.assertEqual("https://example.com/spec?a=1", result["normalized"])

    def test_dingtalk_lookalikes_are_not_treated_as_documents(self):
        # Wrong scheme or wrong host: not a DingTalk document. The first two are
        # rejected as invalid document URLs, the third falls back to a generic URL.
        blocked = (
            "http://alidocs.dingtalk.com/i/nodes/ABCDEFGH1234",
            "https://alidocs.dingtalk.com/i/nodes/short",
            "https://alidocs.dingtalk.com/i/nodes/../ABCDEFGH1234",
        )
        for raw in blocked:
            with self.subTest(raw=raw):
                with self.assertRaises(rs.SourceError) as caught:
                    rs.classify_source(raw)
                self.assertEqual(
                    "requirement_source_url_invalid", caught.exception.reason
                )
        self.assertEqual(
            "url", rs.classify_source("https://evil.example.com/i/nodes/ABCDEFGH1234")["kind"]
        )

    def test_empty_source_is_blocked(self):
        with self.assertRaises(rs.SourceError) as caught:
            rs.classify_source("   ")
        self.assertEqual("requirement_source_unreadable", caught.exception.reason)


class PlanTests(unittest.TestCase):
    def test_plan_emits_argv_lists_without_shell_concatenation(self):
        result = rs.plan_source(DOC_URL, repo=Path("/tmp/repo"), feature="cat-ear")
        captures = [step["capture"] for step in result["steps"]]
        self.assertEqual(["version", "auth", "info", "read", "comments"], captures)
        for step in result["steps"]:
            self.assertIsInstance(step["argv"], list)
            self.assertTrue(all(isinstance(item, str) for item in step["argv"]))
            joined = " ".join(step["argv"])
            self.assertNotIn("&&", joined)
            self.assertNotIn("|", joined)
        info = next(step for step in result["steps"] if step["capture"] == "info")
        self.assertIn(DOC_URL, info["argv"])
        self.assertEqual(1, info["argv"].count(DOC_URL))
        self.assertEqual(
            Path("/tmp/repo/.helixharness/sources/cat-ear").resolve(),
            Path(result["snapshot_dir"]).resolve(),
        )

    def test_text_source_has_no_steps(self):
        result = rs.plan_source("直接的需求描述", repo=Path("/tmp/repo"), feature="x1")
        self.assertEqual([], result["steps"])


class IngestTests(unittest.TestCase):
    def _write(self, directory: Path, name: str, payload: dict) -> Path:
        path = directory / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def _ingest(
        self,
        directory: Path,
        *,
        info=None,
        read=None,
        comments=None,
        version=None,
        fetched_at="2026-11-03T16:00:00+08:00",
        cleanup_inputs=True,
    ):
        return rs.ingest_source(
            repo=directory / "repo",
            feature="cat-ear",
            info_file=self._write(directory, "info.json", info or _info()),
            read_file=self._write(directory, "read.json", read or _payload()),
            comments_file=self._write(directory, "comments.json", comments or _comments()),
            version_file=version,
            fetched_at=fetched_at,
            cleanup_inputs=cleanup_inputs,
        )

    def test_snapshot_records_provenance_without_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            version = root / "version.txt"
            version.write_text("dws version v1.0.63 (abc)\n", encoding="utf-8")
            result = self._ingest(root, version=version)
            snapshot = root / "repo" / ".helixharness" / "sources" / "cat-ear"
            self.assertEqual(snapshot.resolve(), Path(result["snapshot_dir"]).resolve())
            for name in ("document.md", "comments.json", "manifest.json"):
                self.assertTrue((snapshot / name).is_file(), name)
            manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(DOC_URL, manifest["url"])
            self.assertEqual(64, len(manifest["sha256"]))
            self.assertEqual("v1.0.63", manifest["dws_version"])
            self.assertEqual("2026-11-03T16:00:00+08:00", manifest["fetched_at"])
            text = (snapshot / "manifest.json").read_text(encoding="utf-8").casefold()
            for forbidden in ("corp_id", "user_id", "user_name", "token", "access_token"):
                self.assertNotIn(forbidden, text)

    def test_capture_files_are_discarded_after_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self._ingest(root)
            snapshot = Path(result["snapshot_dir"])
            self.assertEqual(
                {"document.md", "comments.json", "manifest.json"},
                {item.name for item in snapshot.iterdir()},
            )
            for name in ("info.json", "read.json", "comments.json"):
                self.assertFalse((root / name).exists(), name)

    def test_capture_files_can_be_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._ingest(root, cleanup_inputs=False)
            for name in ("info.json", "read.json", "comments.json"):
                self.assertTrue((root / name).exists(), name)

    def test_fetched_at_defaults_to_current_utc(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self._ingest(Path(directory), fetched_at="")
            manifest = json.loads(
                (Path(result["snapshot_dir"]) / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertRegex(manifest["fetched_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_success_false_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(rs.SourceError) as caught:
                self._ingest(Path(directory), read=_payload(success=False))
            self.assertEqual("requirement_source_unreadable", caught.exception.reason)

    def test_non_adoc_node_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(rs.SourceError) as caught:
                self._ingest(Path(directory), info=_info(extension="whiteboard"))
            self.assertEqual("requirement_source_unsupported_type", caught.exception.reason)

    def test_empty_markdown_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(rs.SourceError) as caught:
                self._ingest(Path(directory), read=_payload(markdown="   "))
            self.assertEqual("requirement_source_unreadable", caught.exception.reason)

    def test_oversized_document_is_blocked_rather_than_truncated(self):
        with tempfile.TemporaryDirectory() as directory:
            huge = "x" * (rs.MAX_MARKDOWN_BYTES + 1)
            with self.assertRaises(rs.SourceError) as caught:
                self._ingest(Path(directory), read=_payload(markdown=huge))
            self.assertEqual("requirement_source_too_large", caught.exception.reason)

    def test_truncated_comments_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self._ingest(
                Path(directory),
                comments=_comments(commentList=[{"content": "补充需求"}], hasMore=True),
            )
            self.assertEqual(1, result["comment_count"])
            self.assertTrue(result["comments_truncated"])

    def test_symlinked_snapshot_root_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            (repo / ".helixharness").mkdir(parents=True)
            target = root / "elsewhere"
            target.mkdir()
            os.symlink(target, repo / ".helixharness" / "sources")
            with self.assertRaises(rs.SourceError) as caught:
                self._ingest(root)
            self.assertEqual("requirement_source_snapshot_unsafe", caught.exception.reason)


class DiagnoseTests(unittest.TestCase):
    def test_known_failures_map_to_reasons(self):
        cases = (
            ("Error: not logged in", 1, "requirement_source_auth_required"),
            ("token expired, please re-authorize", 1, "requirement_source_auth_required"),
            ("403 forbidden", 1, "requirement_source_forbidden"),
            ("document not found", 1, "requirement_source_not_found"),
            ("", 127, "requirement_source_dws_missing"),
            ("unexpected boom", 1, "requirement_source_unreadable"),
        )
        for stderr, code, expected in cases:
            with self.subTest(stderr=stderr):
                self.assertEqual(expected, rs.diagnose_failure(stderr, code)["reason"])


class CliTests(unittest.TestCase):
    def _run(self, argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = rs.main(argv)
        return code, buffer.getvalue()

    def test_blocked_source_returns_two_with_reason_json(self):
        code, output = self._run(["classify", "--source", "requirements/missing.md"])
        self.assertEqual(2, code)
        self.assertEqual("requirement_source_path_missing", json.loads(output)["reason"])

    def test_valid_source_returns_zero_with_json(self):
        code, output = self._run(["classify", "--source", DOC_URL])
        self.assertEqual(0, code)
        self.assertEqual("dingtalk_doc", json.loads(output)["kind"])


if __name__ == "__main__":
    unittest.main()
