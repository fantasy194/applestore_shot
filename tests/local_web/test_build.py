import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('build_web', Path(__file__).resolve().parents[2] / 'scripts/build_web.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class ReleaseTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows promotion fallback')
    def test_windows_busy_directory_copies_verified_staging(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old, new = root / 'local-web-release', root / 'staging'
            old.mkdir(); new.mkdir()
            (old / 'app').write_text('working')
            (new / 'app').write_text('verified')
            rename = Path.rename
            def busy(source, target):
                if source == new:
                    raise PermissionError('directory open')
                return rename(source, target)
            with patch.object(Path, 'rename', busy):
                build.replace_release(new, old)
            self.assertEqual((old / 'app').read_text(), 'verified')
            self.assertFalse(list(root.glob('.local-web-previous-*')))

    @unittest.skipUnless(os.name == 'nt', 'Windows promotion fallback')
    def test_failed_windows_copy_removes_partial_build_and_restores_old(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old, new = root / 'local-web-release', root / 'staging'
            old.mkdir(); new.mkdir()
            (old / 'app').write_text('working')
            rename = Path.rename
            def busy(source, target):
                if source == new:
                    raise PermissionError('directory open')
                return rename(source, target)
            def partial(source, target, **kwargs):
                (target / 'partial').write_text('incomplete')
                raise OSError('copy failed')
            with patch.object(Path, 'rename', busy), patch.object(build.shutil, 'copytree', partial), self.assertRaises(OSError):
                build.replace_release(new, old)
            self.assertEqual((old / 'app').read_text(), 'working')
            self.assertFalse((old / 'partial').exists())
            self.assertFalse(list(root.glob('.local-web-previous-*')))

    def test_replaces_old_files_without_accumulating_versions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old, new = root / 'local-web-release', root / 'staging'
            old.mkdir(); new.mkdir()
            (old / 'obsolete').write_text('old')
            (new / 'app').write_text('new')
            (root / 'user-data').write_text('keep')
            build.replace_release(new, old)
            self.assertFalse((old / 'obsolete').exists())
            self.assertEqual((old / 'app').read_text(), 'new')
            self.assertEqual((root / 'user-data').read_text(), 'keep')
            self.assertFalse(list(root.glob('.local-web-previous-*')))

    def test_failed_promotion_restores_previous_version(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old, new = root / 'local-web-release', root / 'staging'
            old.mkdir(); new.mkdir()
            (old / 'app').write_text('working')
            rename = Path.rename
            def fail(source, target):
                if source == new:
                    raise OSError('test failure')
                return rename(source, target)
            with patch.object(Path, 'rename', fail), self.assertRaises(OSError):
                build.replace_release(new, old)
            self.assertEqual((old / 'app').read_text(), 'working')
            self.assertFalse(list(root.glob('.local-web-previous-*')))


if __name__ == '__main__':
    unittest.main()
