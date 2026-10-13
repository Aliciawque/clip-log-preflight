"""Reject Windows junctions and reparse metadata using synthetic local fixtures."""

import contextlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import clip_log_preflight as app


class ReparseFixtures(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="clip-reparse-"))
        self.addCleanup(self.cleanup_fixtures)
        self.media = self.root / "media"
        self.media.mkdir()
        self.inputs = self.root / "inputs"
        self.inputs.mkdir()
        self.log = self.inputs / "log.csv"
        self.log.write_text("episode,camera,card,first,last\nE1,A,C1,A001,A001\n",
                            encoding="utf-8")
        self.sources = [("A", "C1", str(self.media))]

    def cleanup_fixtures(self):
        # Junction cleanups run first. Remove only known fixture files and empty
        # directories; never recursively delete a junction target.
        for path in (self.log, self.inputs / "reuse.json", self.inputs / "sentinel.txt",
                     self.media / "sentinel.txt"):
            if path.exists():
                path.unlink()
        self.inputs.rmdir()
        self.media.rmdir()
        self.root.rmdir()

    def assert_reparse(self, result):
        report, code = result
        self.assertEqual(code, 2, report)
        self.assertEqual(report["status"], "error")
        self.assertEqual(report["errors"][0]["code"], "reparse_point", report)
        self.assertNotIn("manifests", report)
        self.assertNotIn("accepted_reuse", report)
        return report


class ReparseMetadataTests(ReparseFixtures):
    def test_reparse_ancestor_rejected_before_input_read_or_scan(self):
        real_lstat = Path.lstat
        def metadata(path):
            if path == self.inputs:
                # No tag: rejection must cover unknown reparse types too.
                return SimpleNamespace(st_mode=stat.S_IFDIR,
                                       st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
            return real_lstat(path)
        with mock.patch.object(Path, "lstat", autospec=True, side_effect=metadata), \
             mock.patch.object(Path, "open", side_effect=AssertionError("must not read input")), \
             mock.patch.object(app.os, "scandir", side_effect=AssertionError("must not scan")):
            report = self.assert_reparse(app.audit(self.log, self.sources))
        self.assertEqual(report["errors"][0]["path"], str(self.inputs))

    def test_reparse_entries_block_before_recursion_or_extension_filter(self):
        for mode, name in [(stat.S_IFDIR, "nested"), (stat.S_IFREG, "ignored.txt")]:
            with self.subTest(name=name):
                entry = SimpleNamespace(name=name, path=str(self.media / name),
                    stat=mock.Mock(return_value=SimpleNamespace(st_mode=mode,
                        st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT
                                           | stat.FILE_ATTRIBUTE_READONLY)))
                with mock.patch.object(app.os, "scandir", side_effect=[
                        contextlib.nullcontext(iter([entry])),
                        AssertionError("must not recurse into reparse entry")]):
                    report = self.assert_reparse(app.audit(self.log, self.sources))
                entry.stat.assert_called_once_with(follow_symlinks=False)
                self.assertEqual(report["errors"][0]["relative_path"], name)

    def test_paths_without_windows_attributes_and_ordinary_attributes_are_accepted(self):
        real_lstat = Path.lstat
        for attributes in ({}, {"st_file_attributes": stat.FILE_ATTRIBUTE_ARCHIVE}):
            with self.subTest(attributes=attributes):
                def metadata(path):
                    return SimpleNamespace(st_mode=real_lstat(path).st_mode, **attributes)
                with mock.patch.object(Path, "lstat", autospec=True, side_effect=metadata):
                    self.assertEqual(app._absolute_path(self.log), self.log)

    def test_symlink_diagnostic_is_preserved(self):
        real_lstat = Path.lstat
        def metadata(path):
            if path == self.inputs:
                return SimpleNamespace(st_mode=stat.S_IFLNK,
                                       st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
            return real_lstat(path)
        with mock.patch.object(Path, "lstat", autospec=True, side_effect=metadata):
            report, code = app.audit(self.log, self.sources)
        self.assertEqual(code, 2)
        self.assertEqual(report["errors"][0]["code"], "symlink")


@unittest.skipUnless(os.name == "nt", "requires native Windows junctions")
class WindowsJunctionTests(ReparseFixtures):
    def junction(self, link, target):
        shell = shutil.which("powershell") or shutil.which("pwsh")
        if shell is None:
            self.skipTest("PowerShell unavailable for creating a temporary junction")
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        command = ("$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path "
                   + quote(link) + " -Target " + quote(target) + " | Out-Null")
        subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command],
                       check=True, capture_output=True, timeout=15)
        # LIFO cleanup removes the junction itself before cleaning its fixture target.
        def remove_junction():
            try:
                os.rmdir(link)
            except FileNotFoundError:
                pass
            self.assertTrue(target.is_dir())
            self.assertEqual(sentinel.read_bytes(), b"synthetic sentinel; no media")
        self.addCleanup(remove_junction)
        sentinel = target / "sentinel.txt"
        sentinel.write_bytes(b"synthetic sentinel; no media")
        info = link.lstat()
        self.assertFalse(stat.S_ISLNK(info.st_mode))
        self.assertTrue(info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return link

    def test_source_root_junction_blocks_before_scan_and_cli_returns_json(self):
        alias = self.junction(self.root / "source-alias", self.media)
        with mock.patch.object(app.os, "scandir", side_effect=AssertionError("must not scan")), \
             mock.patch.object(Path, "open", side_effect=AssertionError("must not read input")):
            self.assert_reparse(app.audit(self.log, [("A", "C1", str(alias))]))
        result = subprocess.run([sys.executable, str(Path(app.__file__).resolve()),
                                 str(self.log), "--source", "A", "C1", str(alias)],
                                capture_output=True, text=True, check=False, timeout=15)
        self.assert_reparse((json.loads(result.stdout), result.returncode))
        self.assertEqual(result.stderr, "")

    def test_embedded_junction_is_not_traversed_even_when_unreferenced(self):
        alias = self.junction(self.media / "nested", self.inputs)
        real_scandir = os.scandir
        def scan(path):
            self.assertEqual(Path(path), self.media, "must not traverse junction target")
            return real_scandir(path)
        with mock.patch.object(app.os, "scandir", side_effect=scan):
            report = self.assert_reparse(app.audit(self.log, self.sources))
        self.assertEqual(report["errors"][0]["relative_path"], alias.name)

    def test_log_junction_ancestor_blocks_before_read(self):
        alias = self.junction(self.root / "log-alias", self.inputs)
        with mock.patch.object(Path, "open", side_effect=AssertionError("must not read input")), \
             mock.patch.object(app.os, "scandir", side_effect=AssertionError("must not scan")):
            self.assert_reparse(app.audit(alias / "log.csv", self.sources))

    def test_policy_junction_ancestor_blocks_before_read(self):
        (self.inputs / "reuse.json").write_text("{}", encoding="utf-8")
        alias = self.junction(self.root / "policy-alias", self.inputs)
        with mock.patch.object(Path, "open", side_effect=AssertionError("must not read input")), \
             mock.patch.object(app.os, "scandir", side_effect=AssertionError("must not scan")):
            self.assert_reparse(app.audit(self.log, self.sources,
                                         reuse_policy=alias / "reuse.json"))

    def test_lexical_parent_components_do_not_hide_a_junction(self):
        alias = self.junction(self.root / "alias", self.media)
        self.assert_reparse(app.audit(self.log, [("A", "C1", str(alias / ".." / "media"))]))

    def test_hidden_and_nonmedia_junctions_are_not_ignored(self):
        for name in (".hidden", "ignored.txt"):
            with self.subTest(name=name):
                alias = self.junction(self.media / name, self.inputs)
                report = self.assert_reparse(app.audit(self.log, self.sources))
                self.assertEqual(report["errors"][0]["relative_path"], name)
                os.rmdir(alias)

    def test_junction_loop_is_rejected_before_repeated_traversal(self):
        self.junction(self.media / "loop", self.media)
        real_scandir = os.scandir
        calls = []
        def scan(path):
            calls.append(Path(path))
            self.assertEqual(calls, [self.media], "must not traverse the loop")
            return real_scandir(path)
        with mock.patch.object(app.os, "scandir", side_effect=scan):
            self.assert_reparse(app.audit(self.log, self.sources))

    def test_broken_junction_is_rejected_without_following_target(self):
        alias = self.junction(self.root / "broken-alias", self.inputs)
        moved = self.root / "moved-inputs"
        self.inputs.rename(moved)
        try:
            self.assert_reparse(app.audit(self.log, [("A", "C1", str(alias))]))
        finally:
            moved.rename(self.inputs)
