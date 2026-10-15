import json
import os
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dashboard_v2_reader as reader

AT = '2026-10-12T11:33:20+00:00'
NOW = int(datetime.fromisoformat(AT).timestamp() * 1000)


def arm():
    return dict(arm='A', days_in=1.07, days_left=19.93, signals=4,
                blocked=1, fills=6, round_trips=3,
                pnl='123.123456789012345678901234567890',
                fees='0.00000000000000000000001', max_dd='0.92',
                loss_limit='10', open_positions=0, feed_errors=9,
                feed_gaps=0, stale_min=0.2)


class DashboardReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = [Path(self.tmp.name) / x for x in ('s', 'j', 'l')]
        self.write(0, (json.dumps({'at': AT, 'arms': [arm()]}) + '\n').encode())
        self.write(1, (json.dumps({'id': 'j', 'body': 'B'}) + '\n').encode())
        self.write(2, json.dumps({'items': [{'id': 'c', 'text': 'todo',
                                            'state': 'OPEN', 'due': None}]}).encode())

    def write(self, i, data):
        self.paths[i].write_bytes(data)

    def read(self, **kw):
        return reader.read_dashboard(*self.paths, now_ms=kw.get('now_ms', NOW),
                                     max_age_ms=kw.get('max_age_ms', 1000),
                                     max_bytes=kw.get('max_bytes', 1048576))

    def test_happy_exact_fields(self):
        result = self.read()
        a = result['arms'][0]
        self.assertEqual(result['scorecard_state'], 'known')
        self.assertEqual(a['pnl']['exact'], arm()['pnl'])
        self.assertEqual(a['fees']['exact'], arm()['fees'])
        self.assertEqual((a['days_in'], a['days_left']), (1.07, 19.93))
        self.assertEqual((a['signals'], a['fills']), (4, 6))
        self.assertEqual(result['journal'][0]['id'], 'j')

    def test_missing_source(self):
        self.paths[0].unlink()
        self.assertEqual(self.read()['scorecard_state'], 'unknown')
        self.assertTrue(any('檔案缺失' in s for s in self.read()['reasons']))

    def test_empty_sources(self):
        for i in range(3):
            self.write(i, b'')
        result = self.read()
        self.assertEqual(result['scorecard_state'], 'unknown')
        self.assertFalse(result['journal'])
        self.assertFalse(result['commitments'])

    def test_corrupt_newest_does_not_fallback(self):
        with self.paths[0].open('ab') as f:
            f.write(b'{bad}\n')
        self.assertEqual(self.read()['scorecard_state'], 'unknown')

    def test_torn_final_ignored(self):
        with self.paths[0].open('ab') as f:
            f.write(b'{unfinished')
        self.assertEqual(self.read()['scorecard_state'], 'known')

    def test_blank_lines(self):
        self.write(0, b'\n \n' + self.paths[0].read_bytes() + b'\n')
        self.assertEqual(self.read()['scorecard_state'], 'known')

    def test_journal_invalid_and_latest_five(self):
        rows = [json.dumps({'id': str(i), 'secret': 'private'}) + '\n'
                for i in range(8)]
        self.write(1, (''.join(rows[:3]) + '{bad}\n' +
                       ''.join(rows[3:]) + 'not finished').encode())
        r = self.read()
        self.assertEqual([v['id'] for v in r['journal']], ['7', '6', '5', '4', '3'])
        self.assertTrue(any('略過' in s for s in r['reasons']))

    def test_tail_boundary_fragment(self):
        self.write(0, b'Z' * 64 + b'\n' + self.paths[0].read_bytes())
        self.assertEqual(self.read(max_bytes=600)['scorecard_state'], 'known')
        self.assertEqual(self.read(max_bytes=12)['scorecard_state'], 'unknown')

    def test_huge_file_bounded_reads(self):
        self.write(0, b'Z' * 1000000 + b'\n' + self.paths[0].read_bytes())
        original = reader.os.read
        count = [0]
        def monitored(fd, size):
            assert size <= 65536
            payload = original(fd, size)
            count[0] += len(payload)
            return payload
        with patch.object(reader.os, 'read', side_effect=monitored):
            result = self.read(max_bytes=500)
        self.assertEqual(result['scorecard_state'], 'known')
        self.assertLessEqual(count[0], 500 + 1 + 500 + 1 + 501)

    def test_oversized_ledger(self):
        self.write(2, b' ' * 1000)
        self.assertFalse(self.read(max_bytes=500)['commitments'])
        self.assertTrue(any('超出' in s for s in self.read(max_bytes=500)['reasons']))

    def test_bad_utf8_complete_window(self):
        self.write(0, b'\xff\n')
        self.assertEqual(self.read()['scorecard_state'], 'unknown')
        self.write(1, b'\xff\n')
        self.assertFalse(self.read()['journal'])

    def test_symlink_refused(self):
        link = Path(self.tmp.name) / 'link'
        link.symlink_to(self.paths[0])
        self.paths[0] = link
        self.assertEqual(self.read()['scorecard_state'], 'unknown')

    def test_directory_fifo_refused(self):
        directory = Path(self.tmp.name) / 'dir'
        directory.mkdir()
        self.paths[0] = directory
        self.assertEqual(self.read()['scorecard_state'], 'unknown')
        if hasattr(os, 'mkfifo'):
            fifo = Path(self.tmp.name) / 'fifo'
            os.mkfifo(fifo)
            self.paths[1] = fifo
            self.assertFalse(self.read()['journal'])

    def test_due_epoch_and_bool(self):
        self.write(2, json.dumps({'items': [
            {'id': 'a', 'state': 'OPEN', 'due': 0},
            {'id': 'b', 'state': 'OPEN', 'due': 1.5},
            {'id': 'c', 'state': 'OPEN', 'due': True},
            {'id': 'd', 'state': 'OPEN', 'due': 'later'}]}).encode())
        items = self.read()['commitments']
        self.assertEqual(items[0]['due'], '1970-01-01T00:00:00+00:00')
        self.assertEqual(items[1]['due'], '1970-01-01T00:00:01.500000+00:00')
        self.assertIsNone(items[2]['due'])
        self.assertEqual(items[3]['due'], 'later')

    def test_no_sensitive_leaks(self):
        self.write(1, b'{"id":"a","secret":"private","path":"hidden"}\n')
        self.write(2, b'{"items":[{"id":"a","text":"todo","state":"OPEN","handle":"hide","command":"hide"}]}')
        result = json.dumps(self.read(), ensure_ascii=False)
        for word in ('private', 'hidden', 'handle', 'command', self.tmp.name):
            self.assertNotIn(word, result)

    def test_stale_future(self):
        self.assertEqual(self.read(now_ms=NOW + 1001)['scorecard_state'], 'unknown')
        self.assertEqual(self.read(now_ms=NOW - 1)['scorecard_state'], 'unknown')

    def test_validation_before_io(self):
        with patch.object(reader, '_bounded_file', side_effect=AssertionError('I/O')):
            for options in ({'max_bytes': True}, {'max_bytes': 0},
                            {'now_ms': True}, {'max_age_ms': -1}):
                with self.subTest(options=options):
                    with self.assertRaises(ValueError):
                        self.read(**options)

    def test_no_writes_and_closed(self):
        before = [(p.stat().st_size, p.stat().st_mtime_ns, p.read_bytes())
                  for p in self.paths]
        original_open = reader.os.open
        opened = []
        def traced(path, flags, *args):
            self.assertFalse(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
            fd = original_open(path, flags, *args)
            opened.append(fd)
            return fd
        with patch.object(reader.os, 'open', side_effect=traced):
            self.read()
        for fd in opened:
            with self.assertRaises(OSError):
                os.fstat(fd)
        self.assertEqual(before, [(p.stat().st_size, p.stat().st_mtime_ns,
                                   p.read_bytes()) for p in self.paths])


if __name__ == '__main__':
    unittest.main()
