"""Bound ambiguous diagnostics without turning partial matches into success."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import clip_log_preflight as app

class AmbiguityBounds(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.media = self.root / 'media'
        self.media.mkdir()
        self.log = self.root / 'log.csv'
        self.log.write_text('episode,camera,card,first,last\nE01,A,C,A001,A001\n')
        self.sources = [['A', 'C', str(self.media)]]

    def copies(self, count):
        for number in reversed(range(count)):
            directory = self.media / f'copy-{number:04}'
            directory.mkdir()
            (directory / 'A001.mov').touch()

    def check(self, count):
        report, code = app.audit(self.log, self.sources)
        self.assertEqual(code, 1)
        self.assertNotIn('manifests', report)
        self.assertNotIn('accepted_reuse', report)
        error = report['errors'][0]
        self.assertEqual(error['code'], 'ambiguous_clip')
        self.assertEqual(error['candidate_count'], count)
        self.assertEqual(error['candidates_truncated'], count > 5)
        self.assertEqual([x['relative_path'] for x in error['candidates']],
                         [f'copy-{n:04}/A001.mov' for n in range(min(count, 5))])
        self.assertEqual(report['counts']['media_files'], count)
        return report

    def test_two_candidates_complete(self):
        self.copies(2); self.check(2)
    def test_at_sample_boundary(self):
        self.copies(5); self.check(5)
    def test_one_over_boundary(self):
        self.copies(6); self.check(6)
    def test_large_sample_deterministic(self):
        self.copies(300)
        self.assertEqual(self.check(300), self.check(300))
    def test_repeated_reuse_diagnostic_amplification(self):
        self.copies(300)
        episodes = [f'E{n:03}' for n in range(100)]
        self.log.write_text('episode,camera,card,first,last\n' + ''.join(
            f'{e},A,C,A001,A001\n' for e in episodes))
        policy = self.root / 'policy.json'
        policy.write_text(json.dumps({'schema_version': 1, 'approvals': [{
            'camera': 'A', 'card': 'C', 'first': 'A001', 'last': 'A001', 'episodes': episodes}]}))
        report, code = app.audit(self.log, self.sources, reuse_policy=policy)
        self.assertEqual(code, 1)
        self.assertEqual(len(report['errors']), 100)
        self.assertNotIn('manifests', report)
        for error in report['errors']:
            self.assertEqual(error['candidate_count'], 300)
            self.assertTrue(error['candidates_truncated'])
            self.assertEqual(len(error['candidates']), 5)
        self.assertLess(len(json.dumps(report, indent=2)), 100_000)
    def test_entry_limit_still_stops_scan(self):
        self.copies(6)
        with mock.patch.object(app, 'MAX_ENTRIES', 7):
            report, code = app.audit(self.log, self.sources)
        self.assertEqual(code, 2)
        self.assertEqual(report['errors'][-1]['code'], 'entry_limit')
        self.assertNotIn('manifests', report)
    def test_late_special_file_still_blocks(self):
        import os
        if not hasattr(os, 'mkfifo'):
            self.skipTest('FIFO unsupported')
        self.copies(6)
        os.mkfifo(self.media / 'zz-special')
        report, code = app.audit(self.log, self.sources)
        self.assertEqual(code, 2)
        self.assertEqual(report['errors'][-1]['code'], 'special_file')
        self.assertNotIn('manifests', report)
    def test_single_and_missing_shapes_unchanged(self):
        report, code = app.audit(self.log, self.sources)
        self.assertEqual(code, 1)
        self.assertNotIn('candidate_count', report['errors'][0])
        self.copies(1)
        report, code = app.audit(self.log, self.sources)
        self.assertEqual(code, 0)
        self.assertEqual(len(report['manifests'][0]['clips']), 1)

if __name__ == '__main__':
    unittest.main()
