"""Independent adversarial tests for the public audit API and CLI.

Only stdlib, local temporary fixtures, and selective OS-failure mocks are used.
Run with: PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -v
"""
from __future__ import annotations

import builtins
import csv
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
import clip_log_preflight as app
from filesystem_fixtures import install_symlink_fixtures


class IndependentPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="clip-log-independent-")
        self.root = Path(self.temp.name)
        self.media = self.root / "media"
        self.media.mkdir()
        self.log = self.root / "log.csv"
        self.source = [("CAM", "CARD", str(self.media))]
        install_symlink_fixtures(self)

    def tearDown(self):
        self.temp.cleanup()

    def rows(self, *rows):
        with self.log.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(app.COLUMNS)
            writer.writerows(rows)

    def clip(self, name, source=None):
        path = (source or self.media) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not media; filename-only fixture\x00")
        return path

    def audit(self, sources=None, extensions=None):
        return app.audit(self.log, self.source if sources is None else sources, extensions)

    def failed(self, result, *, code=None, error=None):
        report, actual = result
        self.assertNotEqual(actual, 0, report)
        if code is not None:
            self.assertEqual(actual, code, report)
        self.assertNotEqual(report["status"], "ready", report)
        self.assertNotIn("manifests", report)
        self.assertTrue(report["errors"], report)
        if error:
            self.assertIn(error, {e["code"] for e in report["errors"]}, report)
        json.dumps(report)
        return report

    def ready(self, result):
        report, code = result
        self.assertEqual(code, 0, report)
        self.assertEqual(report["status"], "ready", report)
        self.assertFalse(report["errors"], report)
        self.assertTrue(report["manifests"], report)
        return report

    def test_inclusive_range_nested_paths_and_order(self):
        self.rows(["E2", "CAM", "CARD", "A009", "A011"],
                  ["E1", "CAM", "CARD", "A001", "A001"])
        for name in ["z/A011.MOV", "a/A010.mp4", "A009.mxf", "A001.MOV"]:
            self.clip(name)
        first = self.ready(self.audit())
        self.assertEqual(first, self.ready(self.audit()))
        self.assertEqual([m["episode"] for m in first["manifests"]], ["E1", "E2"])
        self.assertEqual([c["clip_id"] for c in first["manifests"][1]["clips"]],
                         ["A009", "A010", "A011"])

    def test_case_sensitive_scope(self):
        self.rows(["E", "cam", "CARD", "A001", "A001"])
        self.clip("A001.mov")
        self.failed(self.audit(), code=1, error="unknown_source")

    def test_case_sensitive_clip_id(self):
        self.rows(["E", "CAM", "CARD", "a001", "a001"])
        self.clip("A001.mov")
        self.failed(self.audit(), code=1, error="missing_clip")

    def test_zero_width_preserved(self):
        self.rows(["E", "CAM", "CARD", "A01", "A01"])
        self.clip("A001.mov")
        self.failed(self.audit(), code=1, error="missing_clip")

    def test_different_card_same_id_is_unambiguous(self):
        other = self.root / "other"
        other.mkdir()
        self.rows(["E1", "CAM", "CARD", "A001", "A001"],
                  ["E2", "CAM", "OTHER", "A001", "A001"])
        self.clip("A001.mov")
        self.clip("A001.mov", other)
        report = self.ready(self.audit(self.source + [("CAM", "OTHER", str(other))]))
        self.assertEqual(len(report["manifests"]), 2)

    def test_duplicate_extensions_are_ambiguous(self):
        self.rows(["E", "CAM", "CARD", "A001", "A001"])
        self.clip("A001.mov")
        self.clip("A001.MP4")
        self.failed(self.audit(), code=1, error="ambiguous_clip")

    def test_duplicate_stems_in_subdirectories_are_ambiguous(self):
        self.rows(["E", "CAM", "CARD", "A001", "A001"])
        self.clip("one/A001.mov")
        self.clip("two/A001.MOV")
        self.failed(self.audit(), code=1, error="ambiguous_clip")

    def test_ignored_extension_does_not_match(self):
        self.rows(["E", "CAM", "CARD", "A001", "A001"])
        self.clip("A001.xml")
        self.failed(self.audit(), code=1, error="missing_clip")

    def test_extension_override_replaces_default(self):
        self.rows(["E", "CAM", "CARD", "A001", "A001"])
        self.clip("A001.xml")
        self.clip("A001.mov")
        report = self.ready(self.audit(extensions=["XML"]))
        self.assertEqual(report["manifests"][0]["clips"][0]["relative_path"], "A001.xml")
        self.failed(self.audit(extensions=[]), code=2)

    def test_missing_clip_suppresses_other_valid_episode(self):
        self.rows(["GOOD", "CAM", "CARD", "A001", "A001"],
                  ["BAD", "CAM", "CARD", "A002", "A002"])
        self.clip("A001.mov")
        self.failed(self.audit(), code=1, error="missing_clip")

    def test_duplicate_and_cross_episode_assignment(self):
        self.clip("A001.mov")
        for second_episode, expected in [("E", "duplicate_assignment"), ("OTHER", "cross_episode_reuse")]:
            with self.subTest(second_episode=second_episode):
                self.rows(["E", "CAM", "CARD", "A001", "A001"],
                          [second_episode, "CAM", "CARD", "A001", "A001"])
                self.failed(self.audit(), code=1, error=expected)

    def test_overlapping_ranges_fail(self):
        self.rows(["E", "CAM", "CARD", "A001", "A003"],
                  ["E", "CAM", "CARD", "A003", "A004"])
        self.failed(self.audit(), code=1, error="duplicate_assignment")

    def test_prefix_width_reversed_and_non_numeric_endpoints(self):
        cases = [("A01", "A002", "incompatible_endpoints"),
                 ("A01", "B01", "incompatible_endpoints"),
                 ("A02", "A01", "descending_range"),
                 ("A", "A", "invalid_range"),
                 ("../A001", "../A001", "invalid_range"),
                 ("A\\001", "A\\001", "invalid_range"),
                 ("A１２", "A１２", "invalid_range")]
        for first, last, expected in cases:
            with self.subTest(first=first, last=last):
                self.rows(["E", "CAM", "CARD", first, last])
                self.failed(self.audit(), code=1, error=expected)

    def test_ascii_digits_unicode_prefix_and_large_integer(self):
        for clip in ["0001", "攝影A001", "A" + "9" * 200]:
            with self.subTest(clip=clip):
                self.rows(["E", "CAM", "CARD", clip, clip])
                self.clip(clip + ".mov")
                self.ready(self.audit())

    def test_unicode_normalization_is_not_silently_collapsed(self):
        self.rows(["E", "CAM", "CARD", "é001", "é001"])
        self.clip("e\u0301001.mov")
        self.failed(self.audit(), code=1, error="missing_clip")

    def test_header_faults(self):
        for header in ["", "episode,camera,card,first", "episode,camera,card,first,last,extra",
                       "episode,camera,card,first,first", "camera,episode,card,first,last"]:
            with self.subTest(header=header):
                self.log.write_text(header + "\nE,CAM,CARD,A001,A001\n", encoding="utf-8")
                self.failed(self.audit(), code=2, error="csv_header")

    def test_malformed_csv_and_encoding(self):
        header = "episode,camera,card,first,last\n"
        for body in ['"E,CAM,CARD,A001,A001\n', '"E"junk,CAM,CARD,A001,A001\n',
                     'E,CAM,CARD,A001,A001,extra\n', 'E,CAM,CARD,A001\n', '\n',
                     'E,CAM,CARD,A001,A001\x00\n']:
            with self.subTest(body=body):
                self.log.write_text(header + body, encoding="utf-8")
                self.failed(self.audit(), code=2)
        self.log.write_bytes(b"\xff\xfe" + header.encode())
        self.failed(self.audit(), code=2, error="log_encoding")

    def test_unescaped_quote_in_unquoted_csv_field_is_rejected(self):
        self.clip("A001.mov")
        self.log.write_text('episode,camera,card,first,last\nEP"bad,CAM,CARD,A001,A001\n', encoding="utf-8")
        self.failed(self.audit(), code=2, error="csv_format")

    def test_valid_quoted_comma_and_escaped_quote_round_trip(self):
        episode = 'Episode "1", intro'
        self.rows([episode, "CAM", "CARD", "A001", "A001"])
        self.clip("A001.mov")
        report = self.ready(self.audit())
        self.assertEqual(report["manifests"][0]["episode"], episode)

    def test_scan_cap_stops_lazy_huge_iterator_immediately(self):
        self.rows(["E", "CAM", "CARD", "A001", "A001"])
        visits = []
        fake = mock.Mock()
        fake.name = "unused.txt"
        class Huge:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def __iter__(self):
                for number in range(10**9):
                    visits.append(number)
                    yield fake
        with mock.patch.object(app, "MAX_ENTRIES", 100), \
             mock.patch.object(app.os, "scandir", return_value=Huge()):
            self.failed(self.audit(), code=2, error="entry_limit")
        self.assertEqual(len(visits), 101)
        fake.stat.assert_not_called()

    def test_invalid_paths_and_source_count_are_errors(self):
        self.rows(["E", "CAM", "CARD", "A001", "A001"])
        for root in ["", "bad\x00path", "bad\udcffpath"]:
            with self.subTest(root=repr(root)):
                self.failed(self.audit([("CAM", "CARD", root)]), code=2, error="invalid_path")
        self.failed(self.audit([]), code=2, error="source_count")
        self.failed(self.audit(self.source * 17), code=2, error="source_count")

    def test_utf8_bom_is_supported(self):
        self.rows(["E", "CAM", "CARD", "A001", "A001"])
        self.log.write_bytes(b"\xef\xbb\xbf" + self.log.read_bytes())
        self.clip("A001.mov")
        self.ready(self.audit())

    def test_invalid_field_values_are_blockers(self):
        for episode in ["", " E", "E ", "E\nnext", "E\tvalue", "E\x7f", "E\u0080", "E\u0085middle", "E\u009b1", "E\u009f", "E" * 256]:
            with self.subTest(episode=episode):
                self.rows([episode, "CAM", "CARD", "A001", "A001"])
                self.failed(self.audit(), code=1, error="invalid_field")

    def test_non_directory_and_missing_root(self):
        self.rows(["E", "CAM", "CARD", "A001", "A001"])
        ordinary_file = self.clip("A001.mov")
        self.failed(self.audit([("CAM", "CARD", str(ordinary_file))]), code=2, error="not_directory")
        self.failed(self.audit([("CAM", "CARD", str(self.root / "absent"))]), code=2)

    def test_repeated_scope_or_root_fails(self):
        self.rows(["E", "CAM", "CARD", "A001", "A001"])
        other = self.root / "other"
        other.mkdir()
        self.failed(self.audit(self.source + [("CAM", "CARD", str(other))]), code=2, error="duplicate_source")
        self.failed(self.audit(self.source + [("OTHER", "CARD", str(self.media))]), code=2, error="overlapping_sources")

    def test_overlapping_roots_fail_both_orders(self):
        nested = self.media / "nested"
        nested.mkdir()
        self.rows(["E", "CAM", "CARD", "A001", "A001"])
        sources = self.source + [("OTHER", "CARD", str(nested))]
        for specs in [sources, list(reversed(sources))]:
            self.failed(self.audit(specs), code=2, error="overlapping_sources")

    def test_symlink_root_and_ancestor_rejected(self):
        self.rows(["E", "CAM", "CARD", "A001", "A001"])
        nested = self.media / "nested"
        nested.mkdir()
        link = self.root / "alias"
        link.symlink_to(self.media, target_is_directory=True)
        for path in [link, link / "nested", link / ".." / "media"]:
            with self.subTest(path=path):
                self.failed(self.audit([("CAM", "CARD", str(path))]), code=2, error="symlink")

    def test_live_dangling_nonmedia_and_directory_links_rejected(self):
        self.rows(["E", "CAM", "CARD", "A001", "A001"])
        original = self.clip("A001.mov")
        for target, directory in [(original, False), (self.root / "missing", False), (self.media, True)]:
            link = self.media / "ignored.txt"
            link.symlink_to(target, target_is_directory=directory)
            self.failed(self.audit(), code=2, error="symlink")
            link.unlink()

    def test_log_symlink_rejected(self):
        real = self.root / "real.csv"
        real.write_text("episode,camera,card,first,last\nE,CAM,CARD,A001,A001\n")
        self.log.symlink_to(real)
        self.failed(self.audit(), code=2, error="symlink")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires FIFO support")
    def test_special_files_fail_without_opening(self):
        self.rows(["E", "CAM", "CARD", "A001", "A001"])
        self.clip("A001.mov")
        os.mkfifo(self.media / "unused.txt")
        self.failed(self.audit(), code=2, error="special_file")

    @unittest.skipUnless(os.name == "posix", "requires POSIX undecodable filenames")
    def test_nonunicode_filename_blocks_even_ignored_extension(self):
        self.rows(["E", "CAM", "CARD", "A001", "A001"])
        self.clip("A001.mov")
        fd = os.open(os.fsencode(self.media) + b"/bad-\xff.txt", os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)
        self.failed(self.audit(), code=2, error="filename_encoding")

    def assert_no_internal_identity(self, report):
        forbidden = {"st_dev", "st_ino", "device", "inode", "identity", "identities", "_identity"}
        def inspect(value):
            if isinstance(value, dict):
                self.assertFalse(forbidden.intersection(value), value)
                for child in value.values():
                    inspect(child)
            elif isinstance(value, list):
                for child in value:
                    inspect(child)
        inspect(report)

    @unittest.skipUnless(hasattr(os, "link"), "requires hardlink support")
    def test_hardlinked_ids_cannot_cross_episodes(self):
        self.rows(["GOOD", "CAM", "CARD", "A000", "A000"],
                  ["E1", "CAM", "CARD", "A001", "A001"],
                  ["E2", "CAM", "CARD", "A002", "A002"])
        self.clip("A000.mov")
        original = self.clip("A001.mov")
        os.link(original, self.media / "A002.mov")
        report = self.failed(self.audit(), code=1, error="physical_clip_reuse")
        self.assert_no_internal_identity(report)
        error = next(e for e in report["errors"] if e["code"] == "physical_clip_reuse")
        self.assertEqual(error["previous_episode"], "E1")
        self.assertEqual(error["episode"], "E2")
        self.assertEqual(error["previous_relative_path"], "A001.mov")
        self.assertEqual(error["relative_path"], "A002.mov")

    @unittest.skipUnless(hasattr(os, "link"), "requires hardlink support")
    def test_hardlinked_ids_cannot_repeat_within_episode(self):
        self.rows(["E", "CAM", "CARD", "A001", "A002"])
        original = self.clip("A001.mov")
        os.link(original, self.media / "A002.mov")
        report = self.failed(self.audit(), code=1, error="physical_clip_reuse")
        self.assert_no_internal_identity(report)

    @unittest.skipUnless(hasattr(os, "link"), "requires hardlink support")
    def test_hardlinks_cannot_bypass_camera_or_card_scopes(self):
        other = self.root / "other"
        other.mkdir()
        original = self.clip("A001.mov")
        os.link(original, other / "A001.mov")
        for camera, card in [("CAM", "OTHER"), ("OTHER", "CARD")]:
            with self.subTest(camera=camera, card=card):
                self.rows(["E1", "CAM", "CARD", "A001", "A001"],
                          ["E2", camera, card, "A001", "A001"])
                result = self.audit(self.source + [(camera, card, str(other))])
                report = self.failed(result, code=1, error="physical_clip_reuse")
                self.assert_no_internal_identity(report)

    @unittest.skipUnless(hasattr(os, "link"), "requires hardlink support")
    def test_unrequested_hardlink_alias_does_not_block(self):
        self.rows(["E", "CAM", "CARD", "A001", "A001"])
        original = self.clip("A001.mov")
        os.link(original, self.media / "A002.mov")
        report = self.ready(self.audit())
        self.assert_no_internal_identity(report)
        clip = report["manifests"][0]["clips"][0]
        self.assertEqual(set(clip), {"camera", "card", "clip_id", "record", "source_index", "relative_path"})

    @unittest.skipUnless(hasattr(os, "link"), "requires hardlink support")
    def test_same_stem_hardlinks_still_fail_ambiguity(self):
        self.rows(["E", "CAM", "CARD", "A001", "A001"])
        original = self.clip("A001.mov")
        os.link(original, self.media / "A001.mp4")
        report = self.failed(self.audit(), code=1, error="ambiguous_clip")
        self.assert_no_internal_identity(report)
        candidates = report["errors"][0]["candidates"]
        self.assertTrue(all(set(c) == {"source_index", "relative_path"} for c in candidates))

    def test_unavailable_requested_inode_suppresses_other_matches(self):
        self.rows(["E1", "CAM", "CARD", "A001", "A001"],
                  ["E2", "CAM", "CARD", "A002", "A002"])
        self.clip("A001.mov")
        self.clip("A002.mov")
        real = app._index
        def missing_identity(*args):
            index, counts, identities = real(*args)
            device, _ = identities[(0, "A002.mov")]
            identities[(0, "A002.mov")] = (device, 0)
            return index, counts, identities
        with mock.patch.object(app, "_index", side_effect=missing_identity):
            report = self.failed(self.audit(), code=2, error="file_identity_unavailable")
        self.assert_no_internal_identity(report)

    @unittest.skipUnless(hasattr(os, "link"), "requires hardlink support")
    def test_cli_hardlink_blocker_is_clean_json(self):
        self.rows(["E1", "CAM", "CARD", "A001", "A001"],
                  ["E2", "CAM", "CARD", "A002", "A002"])
        original = self.clip("A001.mov")
        os.link(original, self.media / "A002.mov")
        result = self.cli(self.log, "--source", "CAM", "CARD", self.media)
        self.assertEqual(result.stderr, "")
        report = self.failed((json.loads(result.stdout), result.returncode), code=1, error="physical_clip_reuse")
        self.assert_no_internal_identity(report)

    def test_failed_scandir_is_not_success(self):
        self.rows(["E", "CAM", "CARD", "A001", "A001"])
        self.clip("A001.mov")
        with mock.patch.object(app.os, "scandir", side_effect=PermissionError("simulated denied enumeration")):
            self.failed(self.audit(), code=2, error="io_error")

    def test_recursive_enumeration_failure_blocks_good_match(self):
        self.rows(["E", "CAM", "CARD", "A001", "A001"])
        self.clip("A001.mov")
        denied = self.media / "denied"
        denied.mkdir()
        real = os.scandir
        def scanning(path):
            if Path(path) == denied:
                raise PermissionError("simulated denied nested enumeration")
            return real(path)
        with mock.patch.object(app.os, "scandir", side_effect=scanning):
            self.failed(self.audit(), code=2, error="io_error")

    def test_interrupted_iterator_blocks_partial_scan(self):
        self.rows(["E", "CAM", "CARD", "A001", "A001"])
        self.clip("A001.mov")
        with os.scandir(self.media) as it:
            entry = next(it)
        class Interrupted:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def __iter__(self):
                yield entry
                raise OSError("interrupted enumeration")
        with mock.patch.object(app.os, "scandir", return_value=Interrupted()):
            self.failed(self.audit(), code=2, error="io_error")

    def test_metadata_failure_blocks_partial_scan(self):
        self.rows(["E", "CAM", "CARD", "A001", "A001"])
        fake = mock.Mock(name="DirEntry")
        fake.name = "A001.mov"
        fake.path = str(self.media / "A001.mov")
        fake.stat.side_effect = OSError("vanished during scan")
        iterator = mock.MagicMock()
        iterator.__enter__.return_value = iter([fake])
        with mock.patch.object(app.os, "scandir", return_value=iterator):
            self.failed(self.audit(), code=2, error="io_error")

    def test_entry_cap_bounds_large_scan_with_no_large_fixture(self):
        self.rows(["E", "CAM", "CARD", "A001", "A001"])
        for name in ["A001.mov", "ignored.txt", "ignored2.txt"]:
            self.clip(name)
        with mock.patch.object(app, "MAX_ENTRIES", 2):
            self.failed(self.audit(), code=2, error="entry_limit")

    def test_range_total_row_and_diagnostic_caps(self):
        self.rows(["E", "CAM", "CARD", "A001", "A004"])
        with mock.patch.object(app, "MAX_RANGE", 3):
            self.failed(self.audit(), code=1, error="range_limit")
        with mock.patch.object(app, "MAX_EXPANDED", 3):
            self.failed(self.audit(), code=2, error="expanded_limit")
        self.rows(["E", "CAM", "CARD", "A001", "A001"], ["E", "CAM", "CARD", "A002", "A002"])
        with mock.patch.object(app, "MAX_ROWS", 1):
            self.failed(self.audit(), code=2, error="row_limit")
        with mock.patch.object(app, "MAX_ERRORS", 1):
            self.failed(self.audit(), code=2, error="diagnostic_limit")

    def test_log_size_cap(self):
        self.rows(["E", "CAM", "CARD", "A001", "A001"])
        with mock.patch.object(app, "MAX_LOG_BYTES", 8):
            self.failed(self.audit(), code=2, error="log_size_limit")

    def test_no_media_byte_reads_or_filesystem_mutation(self):
        self.rows(["E", "CAM", "CARD", "A001", "A002"])
        self.clip("A001.mov")
        self.clip("nested/A002.mov")
        def snapshot():
            result = {}
            for path in [self.root, *self.root.rglob("*")]:
                info = path.lstat()
                result[str(path)] = (info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino)
            return result
        before = snapshot()
        real_io_open = io.open
        opened = []
        def guarded_open(file, mode="r", *args, **kwargs):
            path = Path(file)
            opened.append((str(path), mode))
            self.assertEqual(path, self.log, "audit attempted a non-CSV byte read")
            self.assertEqual(mode, "rb", "audit attempted a filesystem write")
            return real_io_open(file, mode, *args, **kwargs)
        with mock.patch.object(app.Path, "open", autospec=True,
                               side_effect=lambda path, mode="r", *args, **kwargs: guarded_open(path, mode, *args, **kwargs)), \
             mock.patch.object(builtins, "open", side_effect=AssertionError("unexpected builtins.open")):
            self.ready(self.audit())
        self.assertEqual(opened, [(str(self.log), "rb")])
        self.assertEqual(before, snapshot())

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", str(PROJECT / "clip_log_preflight.py"), *map(str, args)],
                              cwd=self.root, capture_output=True, text=True, timeout=10,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

    def test_cli_argument_failures_are_json_stdout(self):
        for args in [(), ("--bad-flag",), (str(self.log), "--source", "CAM", "CARD"),
                     (str(self.log), "--sou", "CAM", "CARD", str(self.media))]:
            with self.subTest(args=args):
                result = self.cli(*args)
                self.assertEqual(result.returncode, 2, result)
                self.assertEqual(result.stderr, "")
                self.failed((json.loads(result.stdout), result.returncode), code=2, error="invalid_arguments")

    def test_cli_ready_and_blocked_exit_semantics(self):
        self.rows(["E", "CAM", "CARD", "A001", "A001"])
        args = [str(self.log), "--source", "CAM", "CARD", str(self.media)]
        result = self.cli(*args)
        self.failed((json.loads(result.stdout), result.returncode), code=1, error="missing_clip")
        self.clip("A001.mov")
        before = {str(p) for p in self.root.rglob("*")}
        result = self.cli(*args)
        self.ready((json.loads(result.stdout), result.returncode))
        self.assertEqual(result.stderr, "")
        self.assertEqual(before, {str(p) for p in self.root.rglob("*")})


if __name__ == "__main__":
    unittest.main()
