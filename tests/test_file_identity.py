"""Native hardlinks and deterministic metadata/replacement failure regressions."""

import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import clip_log_preflight as app


class FileIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="clip-identity-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.media = self.root / "media"
        self.media.mkdir()
        self.clip = self.media / "A001.mov"
        self.clip.write_bytes(b"synthetic identity sentinel")
        self.log = self.root / "log.csv"
        self.log.write_text("episode,camera,card,first,last\nE1,A,C1,A001,A001\n", encoding="utf-8")
        self.sources = [("A", "C1", str(self.media))]

    def failed(self, expected):
        report, code = app.audit(self.log, self.sources)
        self.assertEqual(code, 2, report)
        self.assertEqual(report["errors"][0]["code"], expected, report)
        self.assertNotIn("manifests", report)
        self.assertNotIn("accepted_reuse", report)

    def test_distinct_regular_files_across_scopes_do_not_collide(self):
        other = self.root / "other"
        other.mkdir()
        (other / "A001.mov").write_bytes(self.clip.read_bytes())
        self.sources.append(("B", "C2", str(other)))
        with self.log.open("a", encoding="utf-8") as handle:
            handle.write("E2,B,C2,A001,A001\n")
        report, code = app.audit(self.log, self.sources)
        self.assertEqual(code, 0, report)
        self.assertEqual(len(report["manifests"]), 2)

    def test_native_hardlink_alias_across_scopes_still_blocks(self):
        other = self.root / "other"
        other.mkdir()
        os.link(self.clip, other / "A001.mov")
        self.sources.append(("B", "C2", str(other)))
        with self.log.open("a", encoding="utf-8") as handle:
            handle.write("E2,B,C2,A001,A001\n")
        report, code = app.audit(self.log, self.sources)
        self.assertEqual(code, 1, report)
        self.assertEqual(report["errors"][0]["code"], "physical_clip_reuse")
        self.assertNotIn("manifests", report)

    def test_zero_none_or_noninteger_cached_identity_never_produces_ready(self):
        original = app._index
        for identity in (None, (0, 1), (1, 0), (None, 1), (1, None), (1, True), (1, "1")):
            with self.subTest(identity=identity):
                def missing(*args):
                    index, counts, identities = original(*args)
                    identities[(0, "A001.mov")] = identity
                    return index, counts, identities
                with mock.patch.object(app, "_index", side_effect=missing):
                    self.failed("file_identity_unavailable")

    def test_fresh_nonfollowing_stat_rechecks_link_reparse_and_type(self):
        original_stat, original_absolute = os.stat, app._absolute_path
        actual = os.lstat(self.clip)
        for mode, attributes, expected in ((stat.S_IFLNK, 0, "symlink"),
                (stat.S_IFREG, stat.FILE_ATTRIBUTE_REPARSE_POINT, "reparse_point"),
                (stat.S_IFDIR, 0, "source_changed")):
            with self.subTest(expected=expected):
                def metadata(path, *args, **kwargs):
                    if Path(path) == self.clip:
                        self.assertIs(kwargs.get("follow_symlinks"), False)
                        return SimpleNamespace(st_mode=mode, st_file_attributes=attributes,
                                               st_dev=actual.st_dev, st_ino=actual.st_ino)
                    return original_stat(path, *args, **kwargs)
                # The lexical proof was ordinary; simulate replacement just
                # before the subsequent fresh no-follow stat.
                def proof(path):
                    return self.clip if Path(path) == self.clip else original_absolute(path)
                with mock.patch.object(app, "_absolute_path", side_effect=proof), \
                     mock.patch.object(app.os, "stat", side_effect=metadata):
                    self.failed(expected)

    def test_real_file_replacement_after_indexing_suppresses_manifest(self):
        replacement = self.media / "replacement.tmp"
        replacement.write_bytes(b"separate synthetic file")
        original = app._index
        def changed(*args):
            result = original(*args)
            os.replace(replacement, self.clip)
            return result
        with mock.patch.object(app, "_index", side_effect=changed):
            self.failed("source_changed")

    def test_fresh_missing_identity_after_indexing_suppresses_manifest(self):
        original_index, original_stat = app._index, os.stat
        scanned = False
        def indexed(*args):
            nonlocal scanned
            result = original_index(*args)
            scanned = True
            return result
        def missing(path, *args, **kwargs):
            info = original_stat(path, *args, **kwargs)
            if scanned and Path(path) == self.clip:
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0,
                                       st_dev=info.st_dev, st_ino=None)
            return info
        with mock.patch.object(app, "_index", side_effect=indexed), \
             mock.patch.object(app.os, "stat", side_effect=missing):
            self.failed("file_identity_unavailable")

    def test_128_bit_inodes_preserve_high_bits_without_false_alias(self):
        second = self.media / "A002.mov"
        second.touch()
        self.log.write_text("episode,camera,card,first,last\nE1,A,C1,A001,A002\n", encoding="utf-8")
        original = os.stat
        ids = {self.clip: (1 << 100) + 123, second: (1 << 100) + (1 << 64) + 123}
        def large_inode(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if Path(path) in ids:
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0,
                                       st_dev=info.st_dev, st_ino=ids[Path(path)])
            return info
        with mock.patch.object(app.os, "stat", side_effect=large_inode):
            sources = app._sources(self.sources)
            _, _, identities = app._index(sources, app.DEFAULT_EXTENSIONS)
            self.assertEqual(identities[(0, "A001.mov")][1], ids[self.clip])
            self.assertEqual(identities[(0, "A002.mov")][1], ids[second])
            report, code = app.audit(self.log, self.sources)
        self.assertEqual(code, 0, report)
        self.assertEqual(len(report["manifests"][0]["clips"]), 2)

    @unittest.skipUnless(os.name == "nt", "requires native Windows junctions")
    def test_source_replaced_by_junction_after_scan_is_rejected(self):
        shell = shutil.which("powershell") or shutil.which("pwsh")
        self.assertIsNotNone(shell)
        target = self.root / "held-media"
        original = app._index
        replaced = False
        def changed(*args):
            nonlocal replaced
            result = original(*args)
            self.media.rename(target)
            quote = lambda value: "'" + str(value).replace("'", "''") + "'"
            command = ("$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path "
                       + quote(self.media) + " -Target " + quote(target) + " | Out-Null")
            subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command],
                           check=True, capture_output=True, timeout=15)
            replaced = True
            return result
        try:
            with mock.patch.object(app, "_index", side_effect=changed):
                self.failed("reparse_point")
        finally:
            if replaced:
                os.rmdir(self.media)  # Remove only the newly created junction.
            if target.exists():
                target.rename(self.media)
        self.assertEqual(self.clip.read_bytes(), b"synthetic identity sentinel")
