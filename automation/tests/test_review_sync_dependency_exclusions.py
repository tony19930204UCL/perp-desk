"""Regression coverage for excluded Python dependency environments."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import review_sync as m


class DependencyExclusionTests(unittest.TestCase):
    def fixture(self, root):
        lab = root / 'lab'
        research = lab / 'research' / 'offline-r7'
        research.mkdir(parents=True)
        (research / 'analysis.py').write_bytes(b'print("research")\n')
        (research / 'report.md').write_bytes(b'# Report\n')
        env = research / '.venv'
        (env / 'lib' / 'site-packages').mkdir(parents=True)
        (env / 'bin').mkdir()
        (env / 'lib64').symlink_to('lib', target_is_directory=True)
        (env / 'bin' / 'python').symlink_to('missing-python')
        (env / 'lib' / 'site-packages' / 'package.py').write_bytes(b'print(1)\n')
        (env / 'lib' / 'site-packages' / 'README.md').write_bytes(b'# Installed\n')
        (env / 'lib' / 'site-packages' / 'huge.py').write_bytes(b'x' * (2 * 1024 * 1024 + 1))
        (env / 'lib' / 'site-packages' / 'binary.py').write_bytes(b'\x00\x01')
        for name in ('venv', '__pypackages__'):
            folder = research / name
            folder.mkdir()
            (folder / 'package.py').write_bytes(b'\x00')
            (folder / 'lib64').symlink_to('missing', target_is_directory=True)
        return lab, research

    def test_real_collect_skips_installs_and_keeps_research_in_both_modes(self):
        for visibility in ('private', 'public'):
            with self.subTest(visibility=visibility), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                _, research = self.fixture(root)
                files = m.collect(root, visibility=visibility)
                self.assertEqual(files['lab/research/offline-r7/analysis.py'], b'print("research")\n')
                self.assertEqual(files['lab/research/offline-r7/report.md'], b'# Report\n')
                self.assertFalse(any(set(Path(key).parts) & {'.venv', 'venv', '__pypackages__'} for key in files))
                self.assertTrue((research / '.venv' / 'lib64').is_symlink())

    def test_unknown_file_and_directory_symlinks_remain_blocked(self):
        for visibility in ('private', 'public'):
            for name, is_dir in (('unknown.py', False), ('unknown_dir', True)):
                with self.subTest(visibility=visibility, name=name), tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    _, research = self.fixture(root)
                    (research / name).symlink_to('missing-target', target_is_directory=is_dir)
                    with self.assertRaises(m.Blocked):
                        m.collect(root, visibility=visibility)

    def test_near_match_names_are_not_excluded(self):
        for visibility in ('private', 'public'):
            for name in ('.venv-not', 'venv_notes'):
                with self.subTest(visibility=visibility, name=name), tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    _, research = self.fixture(root)
                    folder = research / name
                    folder.mkdir()
                    (folder / 'real.py').write_bytes(b'pass\n')
                    files = m.collect(root, visibility=visibility)
                    self.assertEqual(files['lab/research/offline-r7/' + name + '/real.py'], b'pass\n')
                    (folder / 'lib64').symlink_to('missing', target_is_directory=True)
                    with self.assertRaises(m.Blocked):
                        m.collect(root, visibility=visibility)


if __name__ == '__main__':
    unittest.main()
