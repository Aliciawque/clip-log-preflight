"""Synthetic filename fixtures only: no real video and no third-party packages."""

import contextlib
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import clip_log_preflight as preflight
from filesystem_fixtures import install_symlink_fixtures


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.media = self.root / "media"
        self.media.mkdir()
        self.log = self.root / "log.csv"
        self.specs = [("A", "C1", str(self.media))]
        install_symlink_fixtures(self)

    def log_rows(self, rows):
        with self.log.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(preflight.COLUMNS)
            writer.writerows(rows)

    def files(self, *names, directory=None):
        directory = directory or self.media
        for name in names:
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"")

    def run_audit(self, extensions=None):
        return preflight.audit(self.log, self.specs, extensions)

    def assert_code(self, report, exitcode, expected_exit, diagnostic):
        self.assertEqual(exitcode, expected_exit, report)
        self.assertIn(diagnostic, [error["code"] for error in report["errors"]])
        self.assertNotIn("manifests", report)

    def test_inclusive_endpoints_and_padding(self):
        self.log_rows([["E1", "A", "C1", "A0012", "A0015"]])
        self.files("A0012.mov", "A0013.mov", "A0014.mov", "A0015.mov")
        report, code = self.run_audit()
        self.assertEqual(code, 0, report)
        self.assertEqual(report["status"], "ready")
        self.assertEqual([clip["clip_id"] for clip in report["manifests"][0]["clips"]],
                         ["A0012", "A0013", "A0014", "A0015"])
        self.assertEqual(report["counts"]["media_files"], 4)

    def test_singleton_and_digit_only_id(self):
        self.log_rows([["E1", "A", "C1", "0012", "0012"]])
        self.files("0012.MOV")
        report, code = self.run_audit()
        self.assertEqual(code, 0, report)
        self.assertEqual(report["manifests"][0]["clips"][0]["relative_path"], "0012.MOV")

    def test_camera_and_card_scopes_do_not_collide(self):
        camera_b, card_two = self.root / "camera_b", self.root / "card_two"
        camera_b.mkdir()
        card_two.mkdir()
        self.specs.extend([("B", "C1", str(camera_b)), ("A", "C2", str(card_two))])
        self.log_rows([["E1", "A", "C1", "0001", "0001"],
                       ["E1", "B", "C1", "0001", "0001"],
                       ["E2", "A", "C2", "0001", "0001"]])
        for directory in (self.media, camera_b, card_two):
            self.files("0001.mov", directory=directory)
        report, code = self.run_audit()
        self.assertEqual(code, 0, report)
        self.assertEqual(len(report["manifests"]), 2)
        self.assertEqual([c["source_index"] for c in report["manifests"][0]["clips"]], [0, 1])

    def test_prefix_case_and_zero_width_are_exact(self):
        for filename in ["a001.mov", "A01.mov", "A0001.mov"]:
            with self.subTest(filename=filename):
                self.log_rows([["E1", "A", "C1", "A001", "A001"]])
                self.files(filename)
                self.assert_code(*self.run_audit(), 1, "missing_clip")
                (self.media / filename).unlink()

    def test_unicode_prefix_is_preserved(self):
        self.log_rows([["Épisode", "A", "C1", "撮影009", "撮影010"]])
        self.files("撮影009.mov", "撮影010.mov")
        report, code = self.run_audit()
        self.assertEqual(code, 0, report)
        self.assertEqual(report["manifests"][0]["clips"][0]["clip_id"], "撮影009")

    def test_unicode_digits_are_not_ascii_endpoints(self):
        self.log_rows([["E1", "A", "C1", "A００１", "A００２"]])
        self.assert_code(*self.run_audit(), 1, "invalid_range")

    def test_final_digit_run_only(self):
        self.log_rows([["E1", "A", "C1", "CAM2_TAKE009", "CAM2_TAKE010"]])
        self.files("CAM2_TAKE009.mov", "CAM2_TAKE010.mov")
        report, code = self.run_audit()
        self.assertEqual(code, 0, report)
        self.assertEqual(len(report["manifests"][0]["clips"]), 2)

    def test_no_unicode_normalization(self):
        self.log_rows([["E1", "A", "C1", "é001", "é001"]])
        self.files("e\u0301001.mov")
        self.assert_code(*self.run_audit(), 1, "missing_clip")

    def test_bad_ranges(self):
        cases = [("A002", "A001", "descending_range"),
                 ("A001", "B002", "incompatible_endpoints"),
                 ("A001", "a002", "incompatible_endpoints"),
                 ("A001", "A0002", "incompatible_endpoints"),
                 ("A001x", "A002x", "invalid_range"),
                 ("../A001", "../A002", "invalid_range"),
                 ("x\\A001", "x\\A002", "invalid_range")]
        for first, last, diagnostic in cases:
            with self.subTest(first=first, last=last):
                self.log_rows([["E1", "A", "C1", first, last]])
                self.assert_code(*self.run_audit(), 1, diagnostic)

    def test_empty_or_whitespace_fields(self):
        for value in ("", " A001", "A001 ", "A\n001", "A\t001"):
            with self.subTest(value=value):
                self.log_rows([["E1", "A", "C1", value, "A001"]])
                self.assert_code(*self.run_audit(), 1, "invalid_field")

    def test_unknown_scope(self):
        self.log_rows([["E1", "B", "C1", "A001", "A001"]])
        self.assert_code(*self.run_audit(), 1, "unknown_source")

    def test_same_episode_overlapping_rows(self):
        self.log_rows([["E1", "A", "C1", "A001", "A003"],
                       ["E1", "A", "C1", "A003", "A004"]])
        self.assert_code(*self.run_audit(), 1, "duplicate_assignment")

    def test_cross_episode_reuse_blocks(self):
        self.log_rows([["E1", "A", "C1", "A001", "A001"],
                       ["E2", "A", "C1", "A001", "A001"]])
        self.assert_code(*self.run_audit(), 1, "cross_episode_reuse")

    @unittest.skipUnless(hasattr(os, "link"), "hardlinks unavailable")
    def test_hardlink_alias_assignments_block_within_and_across_episodes(self):
        self.files("A001.mov")
        os.link(self.media / "A001.mov", self.media / "A002.mov")
        for second_episode in ("E1", "E2"):
            with self.subTest(second_episode=second_episode):
                self.log_rows([["E1", "A", "C1", "A001", "A001"],
                               [second_episode, "A", "C1", "A002", "A002"]])
                self.assert_code(*self.run_audit(), 1, "physical_clip_reuse")

    @unittest.skipUnless(hasattr(os, "link"), "hardlinks unavailable")
    def test_hardlink_alias_assignments_across_scopes_block(self):
        other = self.root / "other"
        other.mkdir()
        self.specs.append(("B", "C2", str(other)))
        self.files("A001.mov")
        os.link(self.media / "A001.mov", other / "A001.mov")
        self.log_rows([["E1", "A", "C1", "A001", "A001"],
                       ["E2", "B", "C2", "A001", "A001"]])
        report, code = self.run_audit()
        self.assert_code(report, code, 1, "physical_clip_reuse")
        self.assertEqual(report["errors"][0]["previous_source_index"], 0)
        self.assertEqual(report["errors"][0]["source_index"], 1)

    @unittest.skipUnless(hasattr(os, "link"), "hardlinks unavailable")
    def test_unrequested_hardlink_alias_does_not_block(self):
        self.log_rows([["E1", "A", "C1", "A001", "A001"]])
        self.files("A001.mov")
        os.link(self.media / "A001.mov", self.media / "A002.mov")
        report, code = self.run_audit()
        self.assertEqual(code, 0, report)

    def test_requested_file_without_usable_identity_fails_closed(self):
        self.log_rows([["E1", "A", "C1", "A001", "A001"]])
        self.files("A001.mov")
        original = preflight._index
        def unidentified(*args):
            index, counts, identities = original(*args)
            return index, counts, {path: (device, 0) for path, (device, _) in identities.items()}
        with patch.object(preflight, "_index", side_effect=unidentified):
            self.assert_code(*self.run_audit(), 2, "file_identity_unavailable")

    def test_all_or_nothing_even_when_one_episode_ready(self):
        self.log_rows([["E1", "A", "C1", "A001", "A001"],
                       ["E2", "A", "C1", "A002", "A002"]])
        self.files("A001.mov")
        self.assert_code(*self.run_audit(), 1, "missing_clip")

    def test_duplicate_stems_across_extensions(self):
        self.log_rows([["E1", "A", "C1", "A001", "A001"]])
        self.files("A001.mov", "A001.mp4")
        report, code = self.run_audit()
        self.assert_code(report, code, 1, "ambiguous_clip")
        self.assertEqual(len(report["errors"][0]["candidates"]), 2)

    def test_duplicate_stems_in_subdirectories(self):
        self.log_rows([["E1", "A", "C1", "A001", "A001"]])
        self.files("z/A001.mov", "a/A001.mov")
        report, code = self.run_audit()
        self.assert_code(report, code, 1, "ambiguous_clip")
        self.assertEqual([x["relative_path"] for x in report["errors"][0]["candidates"]],
                         ["a/A001.mov", "z/A001.mov"])

    def test_hidden_files_and_directories_included(self):
        self.log_rows([["E1", "A", "C1", "A001", "A001"],
                       ["E1", "A", "C1", ".A002", ".A002"]])
        self.files(".hidden/A001.mov", ".A002.mov", "A001.txt", "notes.json")
        report, code = self.run_audit()
        self.assertEqual(code, 0, report)
        self.assertEqual(report["counts"]["ignored_files"], 2)
        self.assertEqual(report["counts"]["media_files"], 2)

    def test_unsupported_extension_does_not_match(self):
        self.log_rows([["E1", "A", "C1", "A001", "A001"]])
        self.files("A001.txt")
        self.assert_code(*self.run_audit(), 1, "missing_clip")

    def test_extensions_replace_defaults(self):
        self.log_rows([["E1", "A", "C1", "A001", "A001"]])
        self.files("A001.mov", "A001.CUSTOM")
        report, code = self.run_audit(["custom"])
        self.assertEqual(code, 0, report)
        self.assertEqual(report["extensions"], [".custom"])
        self.assertEqual(report["manifests"][0]["clips"][0]["relative_path"], "A001.CUSTOM")

    def test_bad_extension(self):
        self.log_rows([["E1", "A", "C1", "A001", "A001"]])
        self.assert_code(*self.run_audit(["../mov"]), 2, "invalid_extension")

    def test_duplicate_headers_extra_columns_and_wrong_order(self):
        for header in ("episode,camera,card,first,first", "episode,camera,card,first,last,x",
                       "camera,episode,card,first,last"):
            with self.subTest(header=header):
                self.log.write_text(header + "\nE1,A,C1,A001,A001\n", encoding="utf-8")
                self.assert_code(*self.run_audit(), 2, "csv_header")

    def test_extra_missing_cells_and_blank_record(self):
        for row in ("E1,A,C1,A001,A001,extra", "E1,A,C1,A001", ""):
            with self.subTest(row=row):
                self.log.write_text(",".join(preflight.COLUMNS) + "\n" + row + "\n", encoding="utf-8")
                self.assert_code(*self.run_audit(), 2, "csv_columns")

    def test_malformed_csv(self):
        self.log.write_text(",".join(preflight.COLUMNS) + '\n"E1,A,C1,A001,A001\n', encoding="utf-8")
        self.assert_code(*self.run_audit(), 2, "csv_format")

    def test_escaped_quotes_and_commas_are_valid_csv(self):
        self.specs = [('A"camera', 'C,1', str(self.media))]
        self.log_rows([['E,"1"', 'A"camera', 'C,1', "TAKE001", "TAKE001"]])
        self.files("TAKE001.mov")
        report, code = self.run_audit()
        self.assertEqual(code, 0, report)
        self.assertEqual(report["manifests"][0]["episode"], 'E,"1"')

    def test_quoted_endpoint_is_parsed_without_requiring_an_illegal_windows_filename(self):
        self.log_rows([["E1", "A", "C1", 'TAKE"001', 'TAKE"001']])
        self.assert_code(*self.run_audit(), 1, "missing_clip")

    def test_malformed_quote_position_and_trailing_characters(self):
        for row in ('E"bad,A,C1,A001,A001', '"E1"x,A,C1,A001,A001',
                    '"E1" ,A,C1,A001,A001'):
            with self.subTest(row=row):
                self.log.write_text(",".join(preflight.COLUMNS) + "\n" + row + "\n", encoding="utf-8")
                self.assert_code(*self.run_audit(), 2, "csv_format")

    def test_quoted_headers_and_line_endings(self):
        self.files("A001.mov")
        for newline in ("\n", "\r\n", "\r"):
            with self.subTest(newline=newline):
                self.log.write_bytes(('"episode","camera","card","first","last"' + newline
                                     + '"E1","A","C1","A001","A001"' + newline).encode())
                report, code = self.run_audit()
                self.assertEqual(code, 0, report)

    def test_multiline_csv_field_is_parsed_but_invalid_as_an_id(self):
        self.log_rows([["E1", "A", "C1", "A\n001", "A001"]])
        self.assert_code(*self.run_audit(), 1, "invalid_field")

    def test_utf8_bom(self):
        self.log.write_text(",".join(preflight.COLUMNS) + "\nE1,A,C1,A001,A001\n", encoding="utf-8-sig")
        self.files("A001.mov")
        self.assertEqual(self.run_audit()[1], 0)

    def test_invalid_encoding(self):
        self.log.write_bytes(b"\xff\xfe")
        self.assert_code(*self.run_audit(), 2, "log_encoding")

    def test_nul_in_csv(self):
        self.log.write_bytes(b"episode,camera,card,first,last\nE1,A,C1,A\x0001,A001\n")
        self.assert_code(*self.run_audit(), 2, "csv_format")

    def test_empty_log(self):
        self.log_rows([])
        self.assert_code(*self.run_audit(), 2, "empty_log")

    def test_nonexistent_paths(self):
        self.assert_code(*self.run_audit(), 2, "path_io")
        self.log_rows([["E1", "A", "C1", "A001", "A001"]])
        self.specs = [("A", "C1", str(self.root / "absent"))]
        self.assert_code(*self.run_audit(), 2, "path_io")

    def test_duplicate_source_scopes(self):
        self.specs.append(self.specs[0])
        self.assert_code(*self.run_audit(), 2, "duplicate_source")

    def test_overlapping_source_roots(self):
        child = self.media / "child"
        child.mkdir()
        self.specs.append(("B", "C1", str(child)))
        self.assert_code(*self.run_audit(), 2, "overlapping_sources")
        self.specs.reverse()
        self.assert_code(*self.run_audit(), 2, "overlapping_sources")

    def test_identical_source_roots_different_scope(self):
        self.specs.append(("B", "C1", str(self.media)))
        self.assert_code(*self.run_audit(), 2, "overlapping_sources")

    def test_log_size_limit(self):
        self.log_rows([["E1", "A", "C1", "A001", "A001"]])
        with patch.object(preflight, "MAX_LOG_BYTES", 1):
            self.assert_code(*self.run_audit(), 2, "log_size_limit")

    def test_range_limit(self):
        self.log_rows([["E1", "A", "C1", "A00000", "A10000"]])
        self.assert_code(*self.run_audit(), 1, "range_limit")

    def test_exact_range_and_total_limits_are_inclusive(self):
        self.log_rows([["E1", "A", "C1", "A001", "A002"]])
        self.files("A001.mov", "A002.mov")
        with patch.object(preflight, "MAX_RANGE", 2), patch.object(preflight, "MAX_EXPANDED", 2):
            report, code = self.run_audit()
            self.assertEqual(code, 0, report)

    def test_source_count_limit(self):
        self.log_rows([["E1", "A", "C1", "A001", "A001"]])
        self.files("A001.mov")
        for number in range(1, preflight.MAX_SOURCES):
            directory = self.root / f"card{number}"
            directory.mkdir()
            self.specs.append(("B", str(number), str(directory)))
        report, code = self.run_audit()
        self.assertEqual(code, 0, report)
        self.specs.append(("B", "extra", str(self.root / "extra")))
        self.assert_code(*self.run_audit(), 2, "source_count")

    def test_total_expansion_limit(self):
        self.log_rows([["E1", "A", "C1", "A001", "A003"]])
        with patch.object(preflight, "MAX_EXPANDED", 2):
            self.assert_code(*self.run_audit(), 2, "expanded_limit")

    def test_row_limit(self):
        self.log_rows([["E1", "A", "C1", "A001", "A001"], ["E1", "A", "C1", "A002", "A002"]])
        with patch.object(preflight, "MAX_ROWS", 1):
            self.assert_code(*self.run_audit(), 2, "row_limit")

    def test_entry_limit_counts_unsupported_entries(self):
        self.log_rows([["E1", "A", "C1", "A001", "A001"]])
        self.files("A001.mov", "notes.txt")
        with patch.object(preflight, "MAX_ENTRIES", 1):
            self.assert_code(*self.run_audit(), 2, "entry_limit")

    def test_diagnostic_limit_fails_closed(self):
        self.log_rows([["E1", "A", "C1", "A001", "A003"]])
        with patch.object(preflight, "MAX_ERRORS", 1):
            report, code = self.run_audit()
        self.assert_code(report, code, 2, "diagnostic_limit")
        self.assertEqual(len(report["errors"]), 2)

    def test_scan_permission_error(self):
        self.log_rows([["E1", "A", "C1", "A001", "A001"]])
        with patch.object(preflight.os, "scandir", side_effect=PermissionError("denied")):
            self.assert_code(*self.run_audit(), 2, "io_error")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_media_and_nonmedia_symlinks_block(self):
        self.log_rows([["E1", "A", "C1", "A001", "A001"]])
        self.files("A001.mov")
        for name in ("linked.mov", "linked.txt", "broken.txt"):
            with self.subTest(name=name):
                target = self.media / ("absent" if name == "broken.txt" else "A001.mov")
                link = self.media / name
                link.symlink_to(target)
                self.assert_code(*self.run_audit(), 2, "symlink")
                link.unlink()

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_symlink_source_root_and_ancestor_block(self):
        alias = self.root / "alias"
        alias.symlink_to(self.media, target_is_directory=True)
        child = self.media / "child"
        child.mkdir()
        for path in (alias, alias / "child"):
            self.specs = [("A", "C1", str(path))]
            self.assert_code(*self.run_audit(), 2, "symlink")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO unavailable")
    def test_special_file_blocked_without_opening(self):
        self.log_rows([["E1", "A", "C1", "A001", "A001"]])
        os.mkfifo(self.media / "A001.mov")
        self.assert_code(*self.run_audit(), 2, "special_file")

    def test_deterministic_and_no_log_media_mutations(self):
        self.log_rows([["E2", "A", "C1", "A002", "A002"], ["E1", "A", "C1", "A001", "A001"]])
        self.files("nested/A002.mov", "A001.mov", "notes.txt")
        def snapshot():
            return {str(p.relative_to(self.root)): (p.stat().st_mode, p.stat().st_size,
                    p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else None)
                    for p in self.root.rglob("*")}
        before = snapshot()
        first = self.run_audit()
        second = self.run_audit()
        self.assertEqual(first[1], 0, first)
        self.assertEqual(first, second)
        self.assertEqual(snapshot(), before)
        self.assertEqual([m["episode"] for m in first[0]["manifests"]], ["E1", "E2"])

    def test_media_content_is_not_opened(self):
        self.log_rows([["E1", "A", "C1", "A001", "A001"]])
        self.files("A001.mov")
        original = Path.open
        def restricted_open(path, *args, **kwargs):
            self.assertEqual(path, self.log, "Audit attempted to open media bytes")
            return original(path, *args, **kwargs)
        with patch.object(Path, "open", restricted_open):
            self.assertEqual(self.run_audit()[1], 0)


class CliTests(unittest.TestCase):
    def invoke(self, *arguments):
        script = Path(preflight.__file__).resolve()
        return subprocess.run([sys.executable, str(script), *arguments], capture_output=True,
                              text=True, check=False, timeout=15)

    def test_argument_error_is_json(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout)["errors"][0]["code"], "invalid_arguments")

    def test_help_is_available(self):
        result = self.invoke("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--source", result.stdout)

    def test_cli_ready_and_blocked_json_exit_codes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "media"
            media.mkdir()
            log = root / "log.csv"
            log.write_text("episode,camera,card,first,last\nE1,A,C1,A001,A001\n", encoding="utf-8")
            args = (str(log), "--source", "A", "C1", str(media))
            result = self.invoke(*args)
            self.assertEqual(result.returncode, 1)
            self.assertNotIn("manifests", json.loads(result.stdout))
            (media / "A001.mov").write_bytes(b"")
            result = self.invoke(*args)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual(result.stderr, "")
            self.assertEqual(json.loads(result.stdout)["status"], "ready")

    def test_main_stdout_can_be_captured(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = preflight.main(["--invalid"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "error")


if __name__ == "__main__":
    unittest.main()
