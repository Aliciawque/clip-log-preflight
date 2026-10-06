"""Developer regressions for explicit, bounded cross-episode reuse."""

import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import clip_log_preflight as app


class ReusePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.media = self.root / "media"
        self.media.mkdir()
        self.log = self.root / "log.csv"
        self.policy = self.root / "reuse.json"
        self.sources = [["A", "CARD01", str(self.media)]]
        self.approval = {"camera": "A", "card": "CARD01", "first": "A001", "last": "A002",
                         "episodes": ["E02", "E01"]}
        self.write_log("E02,A,CARD01,A001,A002\nE01,A,CARD01,A001,A002\n")
        self.write_policy()
        for name in ["A001.mov", "A002.mov"]:
            (self.media / name).touch()

    def write_log(self, rows):
        self.log.write_text("episode,camera,card,first,last\n" + rows, encoding="utf-8")

    def write_policy(self, approvals=None):
        self.policy.write_text(json.dumps({"schema_version": 1, "approvals":
                                         [self.approval] if approvals is None else approvals}),
                               encoding="utf-8")

    def run_audit(self):
        return app.audit(self.log, self.sources, reuse_policy=self.policy)

    def assert_failure(self, expected_code=None, exit_code=2):
        report, code = self.run_audit()
        self.assertEqual(code, exit_code, report)
        self.assertNotIn("manifests", report)
        self.assertNotIn("accepted_reuse", report)
        self.assertEqual(report["status"], "error" if code == 2 else "blocked")
        if expected_code:
            self.assertIn(expected_code, [error["code"] for error in report["errors"]], report)
        return report

    def test_exact_range_reuse_preserves_every_assignment(self):
        report, code = self.run_audit()
        self.assertEqual(code, 0, report)
        self.assertEqual([entry["episode"] for entry in report["manifests"]], ["E01", "E02"])
        self.assertEqual(report["accepted_reuse"], [
            {"camera": "A", "card": "CARD01", "clip_id": clip, "episodes": ["E01", "E02"]}
            for clip in ["A001", "A002"]])
        self.assertEqual(report["counts"]["unique_assignments"], 4)
        self.assertEqual(report["counts"]["unique_scoped_clips"], 2)
        self.assertEqual(report["counts"]["requested_physical_files"], 2)

    def test_no_policy_keeps_cross_episode_refusal(self):
        report, code = app.audit(self.log, self.sources)
        self.assertEqual(code, 1)
        self.assertEqual(report["errors"][0]["code"], "cross_episode_reuse")
        self.assertNotIn("accepted_reuse", report)
        self.assertNotIn("unique_scoped_clips", report["counts"])

    def test_unapproved_part_of_range_blocks_every_episode(self):
        self.approval["last"] = "A001"
        self.write_policy()
        self.assert_failure("cross_episode_reuse", 1)

    def test_missing_episode_is_actionable(self):
        self.write_log("E01,A,CARD01,A001,A002\n")
        report = self.assert_failure("reuse_episode_mismatch", 1)
        self.assertEqual(report["errors"][0]["missing_episodes"], ["E02"])

    def test_third_unapproved_episode_blocks(self):
        with self.log.open("a", encoding="utf-8") as handle:
            handle.write("E03,A,CARD01,A001,A002\n")
        report = self.assert_failure("reuse_episode_mismatch", 1)
        self.assertEqual(report["errors"][0]["unapproved_episodes"], ["E03"])

    def test_one_unused_id_in_approved_range_blocks(self):
        self.approval["last"] = "A003"
        self.write_policy()
        report = self.assert_failure("unused_reuse_approval", 1)
        self.assertEqual(report["errors"][0]["clip_id"], "A003")

    def test_duplicates_in_later_episode_still_block(self):
        with self.log.open("a", encoding="utf-8") as handle:
            handle.write("E01,A,CARD01,A001,A002\n")
        report = self.assert_failure("duplicate_assignment", 1)
        self.assertEqual(report["errors"][0]["previous_episode"], "E01")
        self.assertEqual(report["errors"][0]["previous_record"], 3)

    def test_overlapping_approvals_rejected_even_with_same_episodes(self):
        other = {**self.approval, "first": "A002", "last": "A003"}
        self.write_policy([self.approval, other])
        self.assert_failure("reuse_policy_overlap")

    def test_duplicate_approval_rejected(self):
        self.write_policy([self.approval, self.approval])
        self.assert_failure("reuse_policy_overlap")

    def test_schema_version_is_exact_integer(self):
        for value in [True, False, 1.0, "1", 0, 2, None, [], {}]:
            with self.subTest(value=value):
                self.policy.write_text(json.dumps({"schema_version": value, "approvals": [self.approval]}))
                self.assert_failure("reuse_policy_schema")

    def test_unknown_root_fields_rejected(self):
        self.policy.write_text(json.dumps({"schema_version": 1, "approvals": [self.approval],
                                           "allow_all": True}))
        self.assert_failure("reuse_policy_schema")

    def test_missing_and_unknown_approval_fields_rejected(self):
        for field in self.approval:
            with self.subTest(field=field):
                self.write_policy([{key: value for key, value in self.approval.items() if key != field}])
                self.assert_failure("reuse_policy_schema")
        for field in ["allow_all", "episode", "source", "comment"]:
            with self.subTest(field=field):
                self.write_policy([{**self.approval, field: "anything"}])
                self.assert_failure("reuse_policy_schema")

    def test_approval_container_types(self):
        for value in [None, True, 1, "*", {}, []]:
            with self.subTest(value=value):
                self.policy.write_text(json.dumps({"schema_version": 1, "approvals": value}))
                self.assert_failure("reuse_policy_schema")

    def test_approval_element_types(self):
        for value in [None, True, 1, "*", []]:
            with self.subTest(value=value):
                self.write_policy([value])
                self.assert_failure("reuse_policy_schema")

    def test_labels_follow_csv_text_rules(self):
        for field in ["camera", "card", "first", "last"]:
            for value in [None, False, 1, [], {}, "", " A", "A ", "A\x00", "A\x7f", "A\ud800", "A" * 256]:
                with self.subTest(field=field, value=value):
                    self.write_policy([{**self.approval, field: value}])
                    self.assert_failure("reuse_policy_field")

    def test_episodes_strict_and_distinct(self):
        for value in [None, True, {}, "E01", [], ["E01"], ["E01", "E01"], ["E01", 2],
                      ["E01", []], ["E01", ""], ["E01", " E02"], ["E01", "E\x00"],
                      ["E01", "E\ud800"], ["E01", "E" * 256]]:
            with self.subTest(value=value):
                self.write_policy([{**self.approval, "episodes": value}])
                self.assert_failure("reuse_policy_episodes")

    def test_json_duplicate_keys_at_each_level_rejected(self):
        raw = self.policy.read_text()
        for value in [raw.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1'),
                      raw.replace('"camera": "A"', '"camera": "A", "camera": "A"')]:
            with self.subTest(value=value):
                self.policy.write_text(value)
                self.assert_failure("reuse_policy_format")

    def test_nonfinite_json_rejected(self):
        for value in ["NaN", "Infinity", "-Infinity"]:
            with self.subTest(value=value):
                self.policy.write_text('{"schema_version": 1, "approvals": ' + value + '}')
                self.assert_failure("reuse_policy_format")

    def test_malformed_json_and_encoding_rejected(self):
        for value in [b"", b"\xff", b"{", b"{} trailing"]:
            with self.subTest(value=value[:20]):
                self.policy.write_bytes(value)
                self.assert_failure("reuse_policy_format")

    def test_excessively_nested_json_cannot_succeed(self):
        self.policy.write_bytes(b"[" * 2000 + b"]" * 2000)
        self.assert_failure()

    def test_bom_is_accepted(self):
        self.policy.write_bytes(b"\xef\xbb\xbf" + self.policy.read_bytes())
        self.assertEqual(self.run_audit()[1], 0)

    def test_policy_endpoint_validation(self):
        for first, last in [("A001", "A02"), ("A001", "a002"), ("A002", "A001"),
                            ("A", "A"), ("A/001", "A/002"), ("A\\001", "A\\002"),
                            ("A００１", "A００２"), ("A00000", "A10000")]:
            with self.subTest(first=first, last=last):
                self.write_policy([{**self.approval, "first": first, "last": last}])
                self.assert_failure("reuse_policy_range")

    def test_unknown_scope_case_is_not_normalized(self):
        for field, value in [("camera", "a"), ("card", "card01"), ("camera", "*")]:
            with self.subTest(field=field):
                self.write_policy([{**self.approval, field: value}])
                self.assert_failure("reuse_policy_unknown_source")

    def test_episode_wildcard_is_never_expanded(self):
        self.write_policy([{**self.approval, "episodes": ["E01", "*"]}])
        self.assert_failure("reuse_episode_mismatch", 1)

    def test_numeric_ids_with_leading_zeroes(self):
        self.write_log("E01,A,CARD01,001,002\nE02,A,CARD01,001,002\n")
        self.write_policy([{**self.approval, "first": "001", "last": "002"}])
        for name in ["001.mov", "002.mov"]:
            (self.media / name).touch()
        report, code = self.run_audit()
        self.assertEqual(code, 0, report)
        self.assertEqual([clip["clip_id"] for clip in report["accepted_reuse"]], ["001", "002"])

    def test_approval_limit(self):
        with mock.patch.object(app, "MAX_APPROVALS", 0):
            self.assert_failure("policy_approval_limit")

    def test_expanded_limit(self):
        with mock.patch.object(app, "MAX_POLICY_EXPANDED", 1):
            self.assert_failure("policy_expanded_limit")

    def test_assignment_limit_counts_each_episode(self):
        with mock.patch.object(app, "MAX_POLICY_ASSIGNMENTS", 3):
            self.assert_failure("policy_assignment_limit")

    def test_policy_size_limit(self):
        with mock.patch.object(app, "MAX_POLICY_BYTES", self.policy.stat().st_size - 1):
            self.assert_failure("policy_size_limit")

    def test_policy_not_regular(self):
        self.policy = self.root
        self.assert_failure("not_regular_policy")

    def test_missing_policy(self):
        self.policy = self.root / "missing.json"
        self.assert_failure("path_io")

    def test_validation_precedes_source_scan(self):
        self.write_policy([{**self.approval, "episodes": ["E01", "E03"]}])
        with mock.patch.object(app, "_index", side_effect=AssertionError("must not scan")):
            self.assert_failure("reuse_episode_mismatch", 1)

    def test_missing_media_blocks_accepted_reuse_output(self):
        (self.media / "A002.mov").unlink()
        self.assert_failure("missing_clip", 1)

    def test_ambiguous_media_blocks_accepted_reuse_output(self):
        (self.media / "A001.mp4").touch()
        self.assert_failure("ambiguous_clip", 1)

    def test_complete_source_scan_required(self):
        with mock.patch.object(app, "MAX_ENTRIES", 1):
            self.assert_failure("entry_limit")

    def test_only_log_and_policy_bytes_are_opened(self):
        original = Path.open
        opened = []

        def guarded(path, *args, **kwargs):
            self.assertIn(path, [self.log, self.policy])
            opened.append(path)
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "open", guarded):
            self.assertEqual(self.run_audit()[1], 0)
        self.assertEqual(set(opened), {self.log, self.policy})

    def test_deterministic_read_only_audit(self):
        paths = [self.log, self.policy, *self.media.iterdir()]
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
        self.assertEqual(self.run_audit(), self.run_audit())
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths})

    def test_cli_reuse_and_missing_option_value(self):
        command = [sys.executable, str(Path(app.__file__)), str(self.log),
                   "--source", *self.sources[0], "--reuse-policy", str(self.policy)]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(result.stdout)["accepted_reuse"]), 2)
        self.assertEqual(result.stderr, "")
        result = subprocess.run(command[:-1], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["errors"][0]["code"], "invalid_arguments")

    def test_cli_help_explains_policy(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as result:
            app.main(["--help"])
        self.assertEqual(result.exception.code, 0)
        self.assertIn("--reuse-policy", output.getvalue())


if __name__ == "__main__":
    unittest.main()
