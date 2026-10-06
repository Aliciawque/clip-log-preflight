"""Portable symlink fixtures: real links, or explicit Windows metadata simulation.

Only WinError 1314 permits the fallback. All rejection assertions still run;
unexpected filesystem errors propagate and no test is skipped by this helper.
"""

import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace
from unittest import mock


def install_symlink_fixtures(testcase):
    if os.name != "nt":
        return
    real_link, real_lstat, real_scandir = Path.symlink_to, Path.lstat, os.scandir
    simulated = set()

    def link(path, target, target_is_directory=False):
        try:
            return real_link(path, target, target_is_directory=target_is_directory)
        except OSError as error:
            if error.winerror != 1314:
                raise
        path.touch(exist_ok=False)
        simulated.add(path)
        sys.stderr.write(" [Windows symlink metadata simulation: WinError 1314] ")

    def metadata(info):
        fields = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
        fields["st_mode"] = stat.S_IFLNK | stat.S_IMODE(info.st_mode)
        fields["st_file_attributes"] = stat.FILE_ATTRIBUTE_REPARSE_POINT
        return SimpleNamespace(**fields)

    def lstat(path, *args, **kwargs):
        info = real_lstat(path, *args, **kwargs)
        return metadata(info) if path in simulated else info

    class SymlinkEntry:
        def __init__(self, entry):
            self.entry, self.name, self.path = entry, entry.name, entry.path

        def __getattr__(self, name):
            return getattr(self.entry, name)

        def stat(self, *, follow_symlinks=True):
            if follow_symlinks:
                raise AssertionError("must not follow the simulated symlink")
            return metadata(self.entry.stat(follow_symlinks=False))

    class ScandirIterator:
        def __init__(self, path):
            self.iterator = real_scandir(path)

        def __iter__(self):
            return self

        def __next__(self):
            entry = next(self.iterator)
            return SymlinkEntry(entry) if Path(entry.path) in simulated else entry

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

        def close(self):
            self.iterator.close()

    for patcher in (mock.patch.object(Path, "symlink_to", autospec=True, side_effect=link),
                    mock.patch.object(Path, "lstat", autospec=True, side_effect=lstat),
                    mock.patch.object(os, "scandir", side_effect=ScandirIterator)):
        patcher.start()
        testcase.addCleanup(patcher.stop)
