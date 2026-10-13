"""Independent regression checks for bounded ambiguity diagnostic samples."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import clip_log_preflight as app


class AmbiguityReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.media = self.root / "media"
        self.media.mkdir()
        self.log = self.root / "log.csv"
        self.log.write_text(
            "episode,camera,card,first,last\nE01,A,C,A001,A001\n",
            encoding="utf-8",
        )
        self.sources = [["A", "C", str(self.media)]]

    def candidates(self, count, *, root=None, suffix=".mov"):
        root = self.media if root is None else root
        for number in reversed(range(count)):
            directory = root / f"take-{number:03d}"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / ("A001" + suffix)).touch()

    def test_sample_is_scoped_and_preserves_nonzero_source_index(self):
        other = self.root / "other"
        other.mkdir()
        self.candidates(9, root=other)
        self.candidates(6)
        sources = [["B", "C", str(other)], *self.sources]
        report, code = app.audit(self.log, sources)
        self.assertEqual(code, 1)
        error, = report["errors"]
        self.assertEqual(error["candidate_count"], 6)
        self.assertEqual(error["candidates"], [
            {"source_index": 1, "relative_path": f"take-{n:03d}/A001.mov"}
            for n in range(5)
        ])
        self.assertEqual(report["counts"]["media_files"], 15)

    def test_extension_filter_controls_exact_count_and_truncation(self):
        self.candidates(4)
        self.candidates(4, suffix=".MP4")
        complete, code = app.audit(self.log, self.sources, extensions=["mov"])
        self.assertEqual(code, 1)
        error, = complete["errors"]
        self.assertEqual(error["candidate_count"], 4)
        self.assertIs(error["candidates_truncated"], False)
        self.assertEqual(len(error["candidates"]), 4)
        self.assertEqual(complete["counts"]["ignored_files"], 4)
        bounded, code = app.audit(self.log, self.sources)
        self.assertEqual(code, 1)
        error, = bounded["errors"]
        self.assertEqual(error["candidate_count"], 8)
        self.assertIs(error["candidates_truncated"], True)
        self.assertEqual(error["candidates"], [
            {"source_index": 0, "relative_path": path}
            for path in [
                "take-000/A001.MP4", "take-000/A001.mov",
                "take-001/A001.MP4", "take-001/A001.mov",
                "take-002/A001.MP4",
            ]
        ])

    def test_io_failure_after_candidates_still_invalidates_scan(self):
        early = self.media / "z-first"
        self.candidates(7, root=early)
        late = self.media / "a-late"
        late.mkdir()
        real_scandir = os.scandir
        visited = []

        def scandir(path):
            visited.append(Path(path))
            if Path(path) == late:
                raise OSError("independent simulated late enumeration failure")
            return real_scandir(path)

        with mock.patch.object(app.os, "scandir", side_effect=scandir):
            report, code = app.audit(self.log, self.sources)
        self.assertEqual(code, 2)
        self.assertTrue(all(early / f"take-{n:03d}" in visited for n in range(7)))
        self.assertEqual(visited[-1], late)
        self.assertEqual(report["errors"][0]["code"], "io_error")
        self.assertNotIn("manifests", report)

    def test_diagnostic_limit_with_approved_reuse_remains_fail_closed(self):
        self.candidates(6)
        episodes = [f"E{n:03d}" for n in range(101)]
        self.log.write_text(
            "episode,camera,card,first,last\n"
            + "".join(f"{episode},A,C,A001,A001\n" for episode in episodes),
            encoding="utf-8",
        )
        policy = self.root / "policy.json"
        policy.write_text(json.dumps({
            "schema_version": 1,
            "approvals": [{
                "camera": "A", "card": "C", "first": "A001", "last": "A001",
                "episodes": episodes,
            }],
        }), encoding="utf-8")
        report, code = app.audit(self.log, self.sources, reuse_policy=policy)
        self.assertEqual(code, 2)
        self.assertEqual(report["status"], "error")
        self.assertEqual(len(report["errors"]), 101)
        self.assertEqual(report["errors"][-1]["code"], "diagnostic_limit")
        for error in report["errors"][:-1]:
            self.assertEqual(error["code"], "ambiguous_clip")
            self.assertEqual(error["candidate_count"], 6)
            self.assertEqual(len(error["candidates"]), 5)
            self.assertIs(error["candidates_truncated"], True)
        self.assertNotIn("manifests", report)
        self.assertNotIn("accepted_reuse", report)

    def test_unrequested_ambiguous_stems_do_not_block_ready_report(self):
        self.candidates(8)
        (self.media / "A002.mov").touch()
        self.log.write_text(
            "episode,camera,card,first,last\nE01,A,C,A002,A002\n",
            encoding="utf-8",
        )
        report, code = app.audit(self.log, self.sources)
        self.assertEqual(code, 0)
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["counts"]["media_files"], 9)
        self.assertEqual(report["manifests"][0]["clips"][0]["relative_path"], "A002.mov")

    def test_cli_sample_round_trips_unicode_and_escaped_paths(self):
        # Double quotes are not legal in Windows filenames; Unicode still
        # exercises JSON escapes on either platform.
        quote_name = "quote's" if os.name == "nt" else "quote\""
        names = [quote_name, "é", "Ω", "a space", "[copy]", "日本語"]
        for name in names:
            directory = self.media / name
            directory.mkdir()
            (directory / "A001.mov").touch()
        result = subprocess.run(
            [sys.executable, str(Path(app.__file__)), str(self.log),
             "--source", "A", "C", str(self.media)],
            capture_output=True, text=True, encoding="utf-8", check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "")
        report = json.loads(result.stdout)
        error, = report["errors"]
        self.assertEqual(error["candidate_count"], 6)
        self.assertIs(error["candidates_truncated"], True)
        self.assertEqual(
            [candidate["relative_path"] for candidate in error["candidates"]],
            sorted(name + "/A001.mov" for name in names)[:5],
        )
        self.assertNotIn("manifests", report)


if __name__ == "__main__":
    unittest.main()
