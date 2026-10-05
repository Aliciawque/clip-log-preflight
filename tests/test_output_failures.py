"""Output regressions using stdlib streams and synthetic filename fixtures."""

import builtins
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import clip_log_preflight as app


PROJECT = Path(__file__).resolve().parents[1]
ARGUMENTS = ["log.csv", "--source", "A", "C1", "media"]
REPORT = {"schema_version": 1, "status": "ready", "errors": [],
          "manifests": [{"episode": "\u00c9pisode \u651d\u5f71", "clips": []}]}


class RecordingStream(io.StringIO):
    def __init__(self, *, write_error=None, flush_error=None):
        super().__init__()
        self.write_error = write_error
        self.flush_error = flush_error
        self.flushes = 0

    def write(self, text):
        if self.write_error is not None:
            raise self.write_error
        return super().write(text)

    def flush(self):
        self.flushes += 1
        if self.flush_error is not None:
            raise self.flush_error
        super().flush()


class OutputFailureTests(unittest.TestCase):
    def run_main(self, output, *, code=0, report=REPORT, arguments=ARGUMENTS):
        with mock.patch.object(app.sys, "stdout", output), \
             mock.patch.object(app, "audit", return_value=(report, code)):
            result = app.main(arguments)
            self.assertIs(app.sys.stdout, output)
        self.assertFalse(output.closed, "main must not close caller-owned output")
        return result

    def test_successful_write_is_flushed_and_preserves_exit_codes(self):
        for code in (0, 1, 2):
            with self.subTest(code=code):
                output = RecordingStream()
                self.addCleanup(output.close)
                self.assertEqual(self.run_main(output, code=code), code)
                self.assertEqual(output.flushes, 1)
                self.assertEqual(json.loads(output.getvalue()), REPORT)
                self.assertTrue(output.getvalue().endswith("\n"))

    def test_write_failures_return_two_without_closing_output(self):
        for error in (OSError("write failed"), BrokenPipeError("pipe closed"),
                      UnicodeEncodeError("ascii", "\u00e9", 0, 1, "narrow console"),
                      ValueError("I/O operation on closed file")):
            with self.subTest(error=type(error).__name__):
                output = RecordingStream(write_error=error)
                self.addCleanup(output.close)
                self.assertEqual(self.run_main(output), 2)

    def test_deferred_flush_failures_return_two_without_closing_output(self):
        for error in (OSError("flush failed"), BrokenPipeError("pipe closed"),
                      ValueError("I/O operation on closed file")):
            with self.subTest(error=type(error).__name__):
                output = RecordingStream(flush_error=error)
                self.addCleanup(output.close)
                self.assertEqual(self.run_main(output), 2)
                self.assertEqual(output.flushes, 1)
                self.assertEqual(json.loads(output.getvalue()), REPORT)

    def test_closed_caller_stream_returns_two(self):
        output = io.StringIO()
        output.close()
        with mock.patch.object(app.sys, "stdout", output), \
             mock.patch.object(app, "audit", return_value=(REPORT, 0)):
            self.assertEqual(app.main(ARGUMENTS), 2)
            self.assertIs(app.sys.stdout, output)

    def test_missing_stdout_returns_two(self):
        with mock.patch.object(app.sys, "stdout", None):
            self.assertEqual(app.main(ARGUMENTS), 2)
            self.assertEqual(app._cli(), 2)
            self.assertIsNone(app.sys.stdout)

    def test_cli_stream_without_usable_descriptor_returns_two(self):
        for output in (io.StringIO(), object()):
            with self.subTest(output=type(output).__name__):
                with mock.patch.object(app.sys, "stdout", output):
                    self.assertEqual(app._cli(), 2)
                    self.assertIs(app.sys.stdout, output)

    def test_ascii_console_preserves_unicode_via_json_escapes(self):
        raw = io.BytesIO()
        output = io.TextIOWrapper(raw, encoding="ascii", errors="strict")
        self.addCleanup(output.close)
        self.assertEqual(self.run_main(output), 0)
        self.assertEqual(json.loads(raw.getvalue().decode("ascii")), REPORT)
        self.assertIn(b"\\u00c9", raw.getvalue())

    def test_help_success_is_flushed_and_keeps_caller_stream_open(self):
        output = RecordingStream()
        self.addCleanup(output.close)
        with mock.patch.object(app.sys, "stdout", output):
            with self.assertRaises(SystemExit) as result:
                app.main(["--help"])
            self.assertIs(app.sys.stdout, output)
        self.assertEqual(result.exception.code, 0)
        self.assertEqual(output.flushes, 1)
        self.assertIn("--source", output.getvalue())
        self.assertFalse(output.closed)

    def test_help_write_and_flush_failures_return_two(self):
        for stage in ("write_error", "flush_error"):
            with self.subTest(stage=stage):
                output = RecordingStream(**{stage: BrokenPipeError("pipe closed")})
                self.addCleanup(output.close)
                self.assertEqual(self.run_main(output, arguments=["--help"]), 2)

    def test_cli_wrapper_keeps_the_actual_caller_descriptor_open(self):
        with tempfile.TemporaryFile(mode="w+", encoding="ascii", errors="strict") as output:
            descriptor = output.fileno()
            with mock.patch.object(app.sys, "stdout", output), \
                 mock.patch.object(app.sys, "argv", ["clip_log_preflight.py", *ARGUMENTS]), \
                 mock.patch.object(app, "audit", return_value=(REPORT, 0)):
                self.assertEqual(app._cli(), 0)
                self.assertIs(app.sys.stdout, output)
            self.assertFalse(output.closed)
            os.fstat(descriptor)
            output.seek(0)
            self.assertEqual(json.load(output), REPORT)

    def test_cli_wrapper_open_failure_is_controlled(self):
        with mock.patch.object(builtins, "open", side_effect=OSError("cannot wrap stdout")):
            self.assertEqual(app._cli(), 2)

    @unittest.skipUnless(Path("/dev/full").exists(), "requires /dev/full")
    def test_failed_cli_wrapper_is_closed_but_caller_descriptor_is_not(self):
        opened = []
        real_open = builtins.open

        def record_open(*args, **kwargs):
            output = real_open(*args, **kwargs)
            opened.append(output)
            self.assertIs(kwargs.get("closefd"), False)
            return output

        with real_open("/dev/full", "w", encoding="ascii") as caller:
            with mock.patch.object(app.sys, "stdout", caller), \
                 mock.patch.object(app.sys, "argv", ["clip_log_preflight.py", *ARGUMENTS]), \
                 mock.patch.object(app, "audit", return_value=(REPORT, 0)), \
                 mock.patch.object(builtins, "open", side_effect=record_open):
                self.assertEqual(app._cli(), 2)
                self.assertIs(app.sys.stdout, caller)
            self.assertEqual(len(opened), 1)
            self.assertTrue(opened[0].closed)
            self.assertFalse(caller.closed)
            os.fstat(caller.fileno())


class OutputSubprocessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="clip-log-output-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.media = root / "media"
        self.media.mkdir()
        (self.media / "A001.mov").write_bytes(b"")
        self.log = root / "log.csv"
        self.log.write_text("episode,camera,card,first,last\nE,A,C1,A001,A001\n", encoding="utf-8")
        self.arguments = [str(self.log), "--source", "A", "C1", str(self.media)]

    def cli(self, arguments, **kwargs):
        return subprocess.run([sys.executable, "-B", str(PROJECT / "clip_log_preflight.py"),
                               *arguments], stderr=subprocess.PIPE, timeout=10, **kwargs)

    @unittest.skipUnless(Path("/dev/full").exists(), "requires /dev/full")
    def test_small_json_and_help_full_device_return_two_without_shutdown_error(self):
        for arguments in ([], self.arguments, ["--help"]):
            with self.subTest(arguments=arguments):
                with open("/dev/full", "wb", buffering=0) as output:
                    result = self.cli(arguments, stdout=output)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stderr, b"")

    @unittest.skipUnless(os.name == "posix", "requires POSIX pipe descriptors")
    def test_broken_pipe_json_and_help_return_two_without_shutdown_error(self):
        for arguments in ([], self.arguments, ["--help"]):
            with self.subTest(arguments=arguments):
                reader, writer = os.pipe()
                os.close(reader)
                with os.fdopen(writer, "wb", buffering=0) as output:
                    result = self.cli(arguments, stdout=output)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stderr, b"")

    @unittest.skipUnless(os.name == "posix", "requires POSIX descriptor inheritance")
    def test_closed_stdout_at_startup_returns_two_without_traceback(self):
        launcher = ("import os, sys; os.close(sys.stdout.fileno()); "
                    "os.execv(sys.executable, [sys.executable, '-B', *sys.argv[1:]])")
        for arguments in ([], self.arguments, ["--help"]):
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [sys.executable, "-c", launcher,
                     str(PROJECT / "clip_log_preflight.py"), *arguments],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, b"")
                self.assertEqual(result.stderr, b"")

    def test_successful_json_and_help_remain_available(self):
        result = self.cli(self.arguments, stdout=subprocess.PIPE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, b"")
        self.assertEqual(json.loads(result.stdout)["status"], "ready")
        help_result = self.cli(["--help"], stdout=subprocess.PIPE)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertEqual(help_result.stderr, b"")
        self.assertIn(b"--source", help_result.stdout)


if __name__ == "__main__":
    unittest.main()
