"""Independent adversarial review of exact, opt-in cross-episode reuse.

Standard-library tests use synthetic filenames only; no real media or network.
"""
from __future__ import annotations

import builtins
import csv
import io
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
import clip_log_preflight as app


class IndependentScopedReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="clip-reuse-independent-")
        self.root = Path(self.temp.name)
        self.media = self.root / "media"
        self.media.mkdir()
        self.log = self.root / "log.csv"
        self.policy = self.root / "reuse.json"
        self.sources = [("CAM", "CARD", str(self.media))]

    def tearDown(self):
        self.temp.cleanup()

    def rows(self, *rows):
        with self.log.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(app.COLUMNS)
            writer.writerows(rows)

    def row(self, episode, first="A001", last=None, camera="CAM", card="CARD"):
        return [episode, camera, card, first, first if last is None else last]

    def clip(self, name="A001.mov", source=None):
        path = (source or self.media) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic filename fixture; not playable media\x00")
        return path

    def approval(self, first="A001", last=None, episodes=("E1", "E2"), camera="CAM", card="CARD"):
        return {"camera": camera, "card": card, "first": first,
                "last": first if last is None else last, "episodes": list(episodes)}

    def document(self, *approvals):
        return {"schema_version": 1, "approvals": list(approvals or [self.approval()])}

    def policy_write(self, data=None):
        self.policy.write_text(json.dumps(self.document() if data is None else data), encoding="utf-8")

    def valid_fixture(self):
        self.rows(self.row("E1"), self.row("E2"))
        self.clip()
        self.policy_write()

    def audit(self, sources=None, policy=None):
        return app.audit(self.log, self.sources if sources is None else sources,
                         reuse_policy=self.policy if policy is None else policy)

    def ready(self, result):
        report, code = result
        self.assertEqual(code, 0, report)
        self.assertEqual(report["status"], "ready", report)
        self.assertEqual(report["errors"], [])
        self.assertIn("manifests", report)
        json.dumps(report)
        return report

    def failed(self, result, code=None, error=None):
        report, actual_code = result
        self.assertNotEqual(actual_code, 0, report)
        if code is not None:
            self.assertEqual(actual_code, code, report)
        self.assertNotEqual(report["status"], "ready", report)
        self.assertNotIn("manifests", report)
        self.assertNotIn("accepted_reuse", report)
        self.assertTrue(report["errors"], report)
        if error:
            self.assertIn(error, {item["code"] for item in report["errors"]}, report)
        json.dumps(report)
        return report

    def test_exact_two_episode_reuse_is_opt_in_and_evidenced(self):
        self.valid_fixture()
        self.failed(app.audit(self.log, self.sources), 1, "cross_episode_reuse")
        report = self.ready(self.audit())
        self.assertEqual(report["accepted_reuse"], [
            {"camera": "CAM", "card": "CARD", "clip_id": "A001", "episodes": ["E1", "E2"]}])
        self.assertEqual([item["episode"] for item in report["manifests"]], ["E1", "E2"])
        self.assertEqual(report["counts"]["unique_assignments"], 2)
        self.assertEqual(report["counts"]["unique_scoped_clips"], 1)
        self.assertEqual(report["counts"]["requested_physical_files"], 1)
        self.assertEqual(report["counts"]["media_files"], 1)

    def test_none_default_report_is_unchanged(self):
        self.rows(self.row("E1"))
        self.clip()
        plain = self.ready(app.audit(self.log, self.sources))
        self.assertEqual(plain, self.ready(app.audit(self.log, self.sources, reuse_policy=None)))
        self.assertNotIn("accepted_reuse", plain)
        self.assertNotIn("unique_scoped_clips", plain["counts"])
        self.assertNotIn("requested_physical_files", plain["counts"])

    def test_ranges_can_be_split_into_rows_without_weakening_scope(self):
        self.rows(self.row("E2", "A001", "A003"), self.row("E1", "A003"),
                  self.row("E1", "A001", "A002"), self.row("SOLO", "A004"))
        for number in range(1, 5):
            self.clip(f"A{number:03d}.mov")
        self.policy_write(self.document(self.approval("A001", "A003", episodes=("E2", "E1"))))
        report = self.ready(self.audit())
        self.assertEqual(report["counts"]["unique_assignments"], 7)
        self.assertEqual(report["counts"]["unique_scoped_clips"], 4)
        self.assertEqual(report["counts"]["requested_physical_files"], 4)
        self.assertEqual([item["clip_id"] for item in report["accepted_reuse"]], ["A001", "A002", "A003"])
        self.assertTrue(all(item["episodes"] == ["E1", "E2"] for item in report["accepted_reuse"]))

    def test_three_episode_reuse_all_row_orders(self):
        self.clip()
        self.policy_write(self.document(self.approval(episodes=("E3", "E2", "E1"))))
        for order in itertools.permutations(("E1", "E2", "E3")):
            with self.subTest(order=order):
                self.rows(*(self.row(episode) for episode in order))
                report = self.ready(self.audit())
                self.assertEqual(report["counts"]["unique_assignments"], 3)
                self.assertEqual(report["counts"]["requested_physical_files"], 1)
                self.assertEqual(report["accepted_reuse"][0]["episodes"], ["E1", "E2", "E3"])

    def test_same_episode_duplicate_blocks_every_order(self):
        self.clip()
        self.policy_write()
        for order in sorted(set(itertools.permutations(("E1", "E2", "E2")))):
            with self.subTest(order=order):
                self.rows(*(self.row(episode) for episode in order))
                self.failed(self.audit(), 1, "duplicate_assignment")

    def test_actual_episode_set_must_equal_approved_per_clip(self):
        self.clip()
        self.policy_write()
        for episodes in [("E1",), ("E2",), ("E1", "E3"), ("E1", "E2", "E3")]:
            with self.subTest(episodes=episodes):
                self.rows(*(self.row(episode) for episode in episodes))
                self.failed(self.audit(), 1)

    def test_unused_and_partly_unused_policy_ranges_block(self):
        self.clip()
        self.rows(self.row("E1"), self.row("E2"))
        for approvals in [(self.approval("A002"),),
                          (self.approval("A001", "A002"),),
                          (self.approval(), self.approval("A002"))]:
            with self.subTest(approvals=approvals):
                self.policy_write(self.document(*approvals))
                self.failed(self.audit(), 1)

    def test_partial_approval_does_not_allow_unapproved_overlap(self):
        self.rows(self.row("E1", "A001", "A002"), self.row("E2", "A001", "A002"))
        self.clip()
        self.clip("A002.mov")
        self.policy_write()
        self.failed(self.audit(), 1, "cross_episode_reuse")

    def test_per_clip_episode_sets_cannot_be_replaced_by_range_union(self):
        self.rows(self.row("E1", "A001", "A002"), self.row("E2", "A001"), self.row("E3", "A002"))
        self.clip()
        self.clip("A002.mov")
        self.policy_write(self.document(self.approval("A001", "A002", episodes=("E1", "E2", "E3"))))
        self.failed(self.audit(), 1)

    def test_policy_does_not_bypass_missing_ambiguous_or_symlink(self):
        self.rows(self.row("E1"), self.row("E2"))
        self.policy_write()
        self.failed(self.audit(), 1, "missing_clip")
        original = self.clip()
        alias = self.clip("nested/A001.mp4")
        self.failed(self.audit(), 1, "ambiguous_clip")
        alias.unlink()
        link = self.media / "unrelated.txt"
        link.symlink_to(original)
        self.failed(self.audit(), 2, "symlink")

    @unittest.skipUnless(hasattr(os, "link"), "requires hardlink support")
    def test_distinct_hardlink_aliases_block_before_or_after_approved_reuse(self):
        original = self.clip()
        os.link(original, self.media / "A002.mov")
        self.policy_write()
        rows = [self.row("E1"), self.row("E2"), self.row("SOLO", "A002")]
        for order in itertools.permutations(rows):
            with self.subTest(order=order):
                self.rows(*order)
                self.failed(self.audit(), 1, "physical_clip_reuse")

    @unittest.skipUnless(hasattr(os, "link"), "requires hardlink support")
    def test_individually_approved_hardlink_aliases_still_block(self):
        original = self.clip()
        os.link(original, self.media / "A002.mov")
        self.policy_write(self.document(self.approval("A001", "A002")))
        self.rows(self.row("E1", "A001", "A002"), self.row("E2", "A001", "A002"))
        self.failed(self.audit(), 1, "physical_clip_reuse")

    @unittest.skipUnless(hasattr(os, "link"), "requires hardlink support")
    def test_individually_approved_alias_across_scopes_blocks(self):
        original = self.clip()
        other = self.root / "other"
        other.mkdir()
        os.link(original, other / "A001.mov")
        sources = self.sources + [("CAM", "OTHER", str(other))]
        self.rows(self.row("E1"), self.row("E2"), self.row("E1", card="OTHER"), self.row("E2", card="OTHER"))
        self.policy_write(self.document(self.approval(), self.approval(card="OTHER")))
        self.failed(self.audit(sources), 1, "physical_clip_reuse")

    @unittest.skipUnless(hasattr(os, "link"), "requires hardlink support")
    def test_same_stem_hardlink_remains_ambiguous(self):
        self.valid_fixture()
        os.link(self.media / "A001.mov", self.media / "A001.mp4")
        self.failed(self.audit(), 1, "ambiguous_clip")

    def test_zero_inode_blocks_approved_reuse(self):
        self.valid_fixture()
        real = app._index
        def missing(*args):
            index, counts, identities = real(*args)
            for key, (device, _) in identities.items():
                identities[key] = (device, 0)
            return index, counts, identities
        with mock.patch.object(app, "_index", side_effect=missing):
            self.failed(self.audit(), 2, "file_identity_unavailable")

    def test_strict_root_schema_types_and_keys(self):
        self.valid_fixture()
        good = self.document()
        cases = [None, [], "anything", 1, True,
                 {}, {"schema_version": 1}, {"approvals": good["approvals"]},
                 {**good, "schema_version": True}, {**good, "schema_version": 1.0},
                 {**good, "schema_version": "1"}, {**good, "schema_version": 2},
                 {**good, "extra": False}, {**good, "approvals": []},
                 {**good, "approvals": {}}, {**good, "approvals": None},
                 {**good, "approvals": "all"}, {**good, "approvals": [None]}]
        for document in cases:
            with self.subTest(document=document):
                self.policy.write_text(json.dumps(document), encoding="utf-8")
                self.failed(self.audit(), 2)

    def test_strict_approval_types_unknown_and_missing_fields(self):
        self.valid_fixture()
        good = self.approval()
        cases = [{**good, "extra": "yes"}]
        for key in good:
            cases.append({k: v for k, v in good.items() if k != key})
        for key in ["camera", "card", "first", "last"]:
            for value in [None, True, 1, 1.5, [], {}, "", " CAM", "CAM ", "A\n001", "A\u0080001", "A" * 256, "\ud800"]:
                cases.append({**good, key: value})
        for approval in cases:
            with self.subTest(approval=approval):
                self.policy_write(self.document(approval))
                self.failed(self.audit(), 2)

    def test_episode_values_are_exact_unique_valid_text(self):
        self.valid_fixture()
        cases = [None, {}, "E1,E2", [], ["E1"], ["E1", "E1"], ["E1", 2],
                 ["E1", True], ["E1", None], ["E1", ["E2"]], ["E1", " E2"],
                 ["E1", "E2 "], ["E1", "E\n2"], ["E1", "E\u009f2"],
                 ["E1", ""], ["E1", "E" * 256], ["E1", "\ud800"]]
        for episodes in cases:
            with self.subTest(episodes=episodes):
                self.policy_write(self.document({**self.approval(), "episodes": episodes}))
                self.failed(self.audit(), 2)

    def test_duplicate_json_keys_and_nonfinite_constants_rejected(self):
        self.valid_fixture()
        entry = json.dumps(self.approval())
        cases = ['{"schema_version":1,"schema_version":1,"approvals":[' + entry + ']}',
                 '{"schema_version":1,"approvals":[],"approvals":[' + entry + ']}',
                 '{"schema_version":1,"approvals":[' + entry[:-1] + ',"camera":"CAM"}]}',
                 '{"schema_version":1,"approvals":[' + entry[:-1] + ',"episodes":["E1","E2"]}]}',
                 '{"schema_version":NaN,"approvals":[' + entry + ']}',
                 '{"schema_version":Infinity,"approvals":[' + entry + ']}',
                 '{"schema_version":-Infinity,"approvals":[' + entry + ']}']
        for raw in cases:
            with self.subTest(raw=raw):
                self.policy.write_text(raw, encoding="utf-8")
                self.failed(self.audit(), 2)

    def test_malformed_encoding_trailing_and_deep_json_fail_closed(self):
        self.valid_fixture()
        good = json.dumps(self.document()).encode()
        for raw in [b"", b"\xff", b"\xfe\xff", b"{", good + b"{}", good + b"\0",
                    b"[" * 2000 + b"0" + b"]" * 2000]:
            with self.subTest(raw=raw[:30]):
                self.policy.write_bytes(raw)
                self.failed(self.audit(), 2)

    def test_policy_scope_never_case_folds_or_acts_as_wildcard(self):
        self.valid_fixture()
        for camera, card in [("cam", "CARD"), ("CAM", "card"), ("*", "CARD"),
                             ("CAM", "*"), ("ALL", "ALL")]:
            with self.subTest(camera=camera, card=card):
                self.policy_write(self.document(self.approval(camera=camera, card=card)))
                self.failed(self.audit(), 2)
        self.policy_write(self.document(self.approval(episodes=("E1", "*"))))
        self.failed(self.audit(), 1)

    def test_policy_endpoint_validation(self):
        self.valid_fixture()
        cases = [("A01", "A002"), ("A001", "a002"), ("A002", "A001"),
                 ("A", "A"), ("A１２", "A１２"), ("../A001", "../A001"),
                 ("A\\001", "A\\001")]
        for first, last in cases:
            with self.subTest(first=first, last=last):
                self.policy_write(self.document(self.approval(first, last)))
                self.failed(self.audit(), 2)

    def test_overlapping_policy_entries_fail_for_all_episode_sets(self):
        self.valid_fixture()
        for episodes in [("E1", "E2"), ("E3", "E4"), ("E2", "E3")]:
            approvals = [self.approval("A001", "A003"), self.approval("A003", "A004", episodes)]
            for entries in (approvals, list(reversed(approvals))):
                with self.subTest(entries=entries):
                    self.policy_write(self.document(*entries))
                    self.failed(self.audit(), 2)

    def test_case_padding_and_unicode_normalization_are_distinct_scopes(self):
        ids = ["A001", "a001", "A01", "001", "é001", "e\u0301001", "攝影001"]
        rows = []
        approvals = []
        for clip_id in ids:
            self.clip(clip_id + ".mov")
            rows.extend([self.row("E1", clip_id), self.row("E2", clip_id)])
            approvals.append(self.approval(clip_id))
        self.rows(*rows)
        self.policy_write(self.document(*reversed(approvals)))
        report = self.ready(self.audit())
        self.assertEqual(len(report["accepted_reuse"]), len(ids))
        self.assertEqual(report["counts"]["requested_physical_files"], len(ids))
        self.assertEqual([item["clip_id"] for item in report["accepted_reuse"]], sorted(ids))

    def test_episode_unicode_normalization_and_case_are_not_collapsed(self):
        episodes = ("é", "e\u0301", "E", "e")
        self.clip()
        self.rows(*(self.row(episode) for episode in episodes))
        self.policy_write(self.document(self.approval(episodes=episodes)))
        report = self.ready(self.audit())
        self.assertEqual(report["accepted_reuse"][0]["episodes"], sorted(episodes))

    def test_combinatorial_exact_episode_sets_and_duplicates(self):
        self.clip()
        self.clip("A002.mov")
        self.policy_write(self.document(self.approval(), self.approval("A002", episodes=("E2", "E3"))))
        pairs = [(clip_id, episode) for clip_id in ("A001", "A002") for episode in ("E1", "E2", "E3")]
        for multiplicities in itertools.product(range(3), repeat=6):
            with self.subTest(multiplicities=multiplicities):
                rows = [self.row(episode, clip_id) for (clip_id, episode), count in zip(pairs, multiplicities)
                        for _ in range(count)]
                self.rows(*rows)
                result = self.audit()
                expected_ready = multiplicities == (1, 1, 0, 0, 1, 1)
                if expected_ready:
                    self.ready(result)
                else:
                    self.failed(result)

    def test_policy_utf8_bom_and_surrounding_json_whitespace(self):
        self.valid_fixture()
        self.policy.write_bytes(b"\xef\xbb\xbf \r\n" + self.policy.read_bytes() + b"\t\n")
        self.ready(self.audit())

    def test_distinct_camera_card_files_are_independently_approved(self):
        self.valid_fixture()
        other = self.root / "other"
        other.mkdir()
        self.clip(source=other)
        self.rows(self.row("E1"), self.row("E2"), self.row("E2", card="OTHER"), self.row("E3", card="OTHER"))
        self.policy_write(self.document(self.approval(), self.approval(card="OTHER", episodes=("E2", "E3"))))
        report = self.ready(self.audit(self.sources + [("CAM", "OTHER", str(other))]))
        self.assertEqual(report["counts"]["requested_physical_files"], 2)
        self.assertEqual(report["counts"]["unique_assignments"], 4)
        self.assertEqual(len(report["accepted_reuse"]), 2)

    def test_accepted_reuse_evidence_accounts_for_every_repeated_manifest_entry(self):
        episodes = tuple(f"E{i:03d}" for i in range(20))
        for n in range(1, 4):
            self.clip(f"A{n:03d}.mov")
        self.rows(*(self.row(episode, "A001", "A003") for episode in episodes))
        self.policy_write(self.document(self.approval("A001", "A003", episodes=episodes)))
        report = self.ready(self.audit())
        actual = {}
        for manifest in report["manifests"]:
            for clip in manifest["clips"]:
                actual.setdefault((clip["camera"], clip["card"], clip["clip_id"]), []).append(manifest["episode"])
        evidenced = {(entry["camera"], entry["card"], entry["clip_id"]): entry["episodes"]
                     for entry in report["accepted_reuse"]}
        self.assertEqual(evidenced, {key: sorted(value) for key, value in actual.items()})
        self.assertEqual(sum(map(len, evidenced.values())), 60)

    def test_adjacent_policy_ranges_are_allowed(self):
        for n in range(1, 5):
            self.clip(f"A{n:03d}.mov")
        self.rows(self.row("E1", "A001", "A004"), self.row("E2", "A001", "A004"))
        self.policy_write(self.document(self.approval("A001", "A002"), self.approval("A003", "A004")))
        self.ready(self.audit())

    def test_policy_caps_apply_at_boundaries(self):
        self.rows(self.row("E1", "A001", "A002"), self.row("E2", "A001", "A002"))
        self.clip()
        self.clip("A002.mov")
        self.policy_write(self.document(self.approval("A001", "A002")))
        for constant, exact, over in [("MAX_RANGE", 2, 1), ("MAX_POLICY_EXPANDED", 2, 1),
                                     ("MAX_POLICY_ASSIGNMENTS", 4, 3)]:
            with self.subTest(constant=constant):
                with mock.patch.object(app, constant, exact):
                    self.ready(self.audit())
                with mock.patch.object(app, constant, over):
                    self.failed(self.audit(), 2)
        self.policy_write(self.document(self.approval(), self.approval("A002")))
        with mock.patch.object(app, "MAX_APPROVALS", 2):
            self.ready(self.audit())
        with mock.patch.object(app, "MAX_APPROVALS", 1):
            self.failed(self.audit(), 2)

    def test_many_episodes_count_toward_policy_assignment_cap(self):
        episodes = tuple(f"E{i:03d}" for i in range(20))
        self.clip()
        self.rows(*(self.row(ep) for ep in episodes))
        self.policy_write(self.document(self.approval(episodes=episodes)))
        with mock.patch.object(app, "MAX_POLICY_ASSIGNMENTS", 20):
            self.ready(self.audit())
        with mock.patch.object(app, "MAX_POLICY_ASSIGNMENTS", 19):
            self.failed(self.audit(), 2)

    def test_full_cap_unicode_policy_diagnostics_are_sampled_and_bounded(self):
        # Regression: this <1 MiB policy previously produced >300 MiB of JSON.
        episodes = tuple("攝" * 248 + f"{number:07d}" for number in range(1000))
        document = self.document(self.approval("A001", "A100", episodes=episodes))
        self.policy.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        self.assertLess(self.policy.stat().st_size, 1024 * 1024)
        self.rows(self.row("UNAPPROVED"))
        with mock.patch.object(app.os, "scandir", side_effect=AssertionError("mismatch must precede scan")):
            report = self.failed(self.audit(), 1)
        self.assertEqual(len(report["errors"]), 100)
        self.assertLess(len(json.dumps(report, ensure_ascii=True, indent=2)), 2 * 1024 * 1024)
        for error in report["errors"]:
            self.assertEqual(error["approved_episode_count"], 1000)
            self.assertEqual(error["missing_episode_count"], 1000)
            self.assertTrue(error["approved_episodes_truncated"])
            self.assertTrue(error["missing_episodes_truncated"])
            self.assertEqual(error["approved_episodes"], sorted(episodes)[:app.MAX_DIAGNOSTIC_EPISODES])
            self.assertEqual(error["missing_episodes"], sorted(episodes)[:app.MAX_DIAGNOSTIC_EPISODES])
            for key in ["actual_episodes", "unapproved_episodes"]:
                self.assertFalse(error[key + "_truncated"])
            expected_count = 1 if error["clip_id"] == "A001" else 0
            self.assertEqual(error["actual_episode_count"], expected_count)
            self.assertEqual(error["unapproved_episode_count"], expected_count)

    def test_diagnostic_cap_stops_stale_policy_without_partial_evidence(self):
        self.rows(self.row("E1", "A999"))
        self.policy_write(self.document(self.approval("A001", "A004")))
        with mock.patch.object(app, "MAX_ERRORS", 2):
            report = self.failed(self.audit(), 2, "diagnostic_limit")
        self.assertEqual(len(report["errors"]), 3)

    def test_aggregate_policy_caps_span_multiple_approvals(self):
        self.valid_fixture()
        self.policy_write(self.document(self.approval("A001", "A002"), self.approval("A003", "A004")))
        with mock.patch.object(app, "MAX_POLICY_EXPANDED", 3):
            self.failed(self.audit(), 2)
        with mock.patch.object(app, "MAX_POLICY_ASSIGNMENTS", 7):
            self.failed(self.audit(), 2)

    def test_byte_cap_exact_boundary_and_statted_oversize(self):
        self.valid_fixture()
        length = self.policy.stat().st_size
        with mock.patch.object(app, "MAX_POLICY_BYTES", length):
            self.ready(self.audit())
        with mock.patch.object(app, "MAX_POLICY_BYTES", length - 1):
            self.failed(self.audit(), 2)

    def test_byte_cap_rechecked_after_file_growth_with_bounded_read(self):
        self.valid_fixture()
        raw = self.policy.read_bytes()
        limit = len(raw)
        read_sizes = []
        class GrowingFile(io.BytesIO):
            def read(self, size=-1):
                read_sizes.append(size)
                return super().read(size)
        real = Path.open
        def opened(path, *args, **kwargs):
            if path == self.policy:
                return GrowingFile(raw + b" " * 1000)
            return real(path, *args, **kwargs)
        with mock.patch.object(app, "MAX_POLICY_BYTES", limit), \
             mock.patch.object(Path, "open", autospec=True, side_effect=opened):
            self.failed(self.audit(), 2)
        self.assertEqual(read_sizes, [limit + 1])

    def test_policy_symlink_and_ancestor_symlink_rejected(self):
        self.valid_fixture()
        link = self.root / "link.json"
        link.symlink_to(self.policy)
        self.failed(self.audit(policy=link), 2, "symlink")
        directory_link = self.root / "alias"
        directory_link.symlink_to(self.root, target_is_directory=True)
        self.failed(self.audit(policy=directory_link / "reuse.json"), 2, "symlink")

    def test_policy_missing_directory_and_unreadable_errors(self):
        self.valid_fixture()
        self.failed(self.audit(policy=self.root / "missing.json"), 2)
        self.failed(self.audit(policy=self.media), 2)
        real = Path.open
        def denied(path, *args, **kwargs):
            if path == self.policy:
                raise PermissionError("independent simulated policy read failure")
            return real(path, *args, **kwargs)
        with mock.patch.object(Path, "open", autospec=True, side_effect=denied):
            self.failed(self.audit(), 2)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires FIFO support")
    def test_fifo_policy_rejected_without_opening(self):
        self.valid_fixture()
        fifo = self.root / "policy.fifo"
        os.mkfifo(fifo)
        self.failed(self.audit(policy=fifo), 2)

    def test_input_failures_do_not_scan_sources(self):
        self.valid_fixture()
        for content in ["invalid", json.dumps(self.document(self.approval("A002")))]:
            self.policy.write_text(content)
            with mock.patch.object(app.os, "scandir", side_effect=AssertionError("input blocker must precede scan")):
                self.failed(self.audit())

    def test_policy_does_not_disable_log_or_scan_limits(self):
        self.valid_fixture()
        with mock.patch.object(app, "MAX_EXPANDED", 1):
            self.failed(self.audit(), 2)
        with mock.patch.object(app, "MAX_ENTRIES", 0):
            self.failed(self.audit(), 2)
        with mock.patch.object(app.os, "scandir", side_effect=OSError("interrupted")):
            self.failed(self.audit(), 2)

    def test_no_media_byte_reads_or_input_mutation_with_policy(self):
        self.valid_fixture()
        def snapshot():
            return {str(path): (path.lstat().st_mode, path.lstat().st_size,
                               path.lstat().st_mtime_ns, path.lstat().st_ctime_ns,
                               path.lstat().st_ino)
                    for path in [self.root, *self.root.rglob("*")]}
        before = snapshot()
        real = Path.open
        opened_paths = []
        def guarded(path, mode="r", *args, **kwargs):
            self.assertIn(path, (self.log, self.policy), "unexpected media/non-input byte read")
            self.assertEqual(mode, "rb", "unexpected filesystem write")
            opened_paths.append(path)
            return real(path, mode, *args, **kwargs)
        with mock.patch.object(Path, "open", autospec=True, side_effect=guarded), \
             mock.patch.object(builtins, "open", side_effect=AssertionError("unexpected builtins.open")):
            self.ready(self.audit())
        self.assertCountEqual(opened_paths, [self.log, self.policy])
        self.assertEqual(before, snapshot())

    def test_accepted_evidence_and_manifest_do_not_disclose_inode_metadata(self):
        self.valid_fixture()
        report = self.ready(self.audit())
        forbidden = {"identity", "identities", "st_dev", "st_ino", "inode", "device"}
        def inspect(value):
            if isinstance(value, dict):
                self.assertFalse(forbidden.intersection(value))
                for child in value.values():
                    inspect(child)
            elif isinstance(value, list):
                for child in value:
                    inspect(child)
        inspect(report)

    def cli(self, *extra):
        return subprocess.run([sys.executable, "-B", str(PROJECT / "clip_log_preflight.py"),
                               str(self.log), "--source", "CAM", "CARD", str(self.media),
                               *map(str, extra)], capture_output=True, text=True, timeout=10,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

    def test_cli_opt_in_is_clean_json_and_deterministic(self):
        self.valid_fixture()
        first = self.cli("--reuse-policy", self.policy)
        second = self.cli("--reuse-policy", self.policy)
        self.ready((json.loads(first.stdout), first.returncode))
        self.assertEqual(first.stderr, "")
        self.assertEqual(first.stdout, second.stdout)
        legacy = self.cli()
        self.failed((json.loads(legacy.stdout), legacy.returncode), 1, "cross_episode_reuse")

    def test_cli_invalid_policy_and_missing_option_argument(self):
        self.valid_fixture()
        for extra in [("--reuse-policy",), ("--reuse-policy", self.root / "absent.json"),
                      ("--reuse-pol", self.policy)]:
            result = self.cli(*extra)
            self.assertEqual(result.stderr, "")
            self.failed((json.loads(result.stdout), result.returncode), 2)
        self.policy.write_text('{"schema_version":1,"schema_version":1,"approvals":[]}')
        result = self.cli("--reuse-policy", self.policy)
        self.assertEqual(result.stderr, "")
        self.failed((json.loads(result.stdout), result.returncode), 2)


if __name__ == "__main__":
    unittest.main()
