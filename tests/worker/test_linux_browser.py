"""Linux prefers Chrome/Edge and supports Debian's native GUI Chromium."""
from pathlib import Path
import unittest
from unittest.mock import patch

import platform_support


class LinuxBrowserTests(unittest.TestCase):
    def test_linux_prefers_installed_google_chrome(self):
        stable = Path('/usr/bin/google-chrome-stable')
        with patch.object(platform_support.sys, 'platform', 'linux'), \
                patch.object(Path, 'is_file', return_value=True):
            self.assertEqual(platform_support.find_browser(), stable)

    def test_linux_uses_google_chrome_when_stable_alias_is_missing(self):
        chrome = Path('/usr/bin/google-chrome')
        with patch.object(platform_support.sys, 'platform', 'linux'), \
                patch.object(Path, 'is_file', autospec=True,
                             side_effect=lambda path: path == chrome):
            self.assertEqual(platform_support.find_browser(), chrome)

    def test_linux_reports_missing_browser(self):
        with patch.object(platform_support.sys, 'platform', 'linux'), \
                patch.object(Path, 'is_file', return_value=False):
            with self.assertRaisesRegex(ValueError, '未找到 Chrome 或 Edge'):
                platform_support.find_browser()

    def test_linux_falls_back_to_debian_chromium(self):
        chromium = Path('/usr/bin/chromium')
        with patch.object(platform_support.sys, 'platform', 'linux'), \
                patch.object(Path, 'is_file', autospec=True,
                             side_effect=lambda path: path == chromium):
            self.assertEqual(platform_support.find_browser(), chromium)

    def test_linux_prefers_edge_over_chromium(self):
        edge = Path('/usr/bin/microsoft-edge')
        chromium = Path('/usr/bin/chromium')
        with patch.object(platform_support.sys, 'platform', 'linux'), \
                patch.object(Path, 'is_file', autospec=True,
                             side_effect=lambda path: path in (edge, chromium)):
            self.assertEqual(platform_support.find_browser(), edge)

    def test_unknown_platform_does_not_get_linux_paths(self):
        self.assertEqual(platform_support.browser_candidates('freebsd'), [])


if __name__ == '__main__':
    unittest.main()
