import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import screenshot


class ScreenshotTest(unittest.TestCase):
    def test_signal_number(self):
        self.assertEqual(screenshot.signal_number("RTMIN"), int(screenshot.signal.SIGRTMIN))
        self.assertEqual(screenshot.signal_number("SIGUSR1"), int(screenshot.signal.SIGUSR1))
        self.assertEqual(screenshot.signal_number("10"), 10)

    def test_auto_without_display_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(screenshot.ScreenshotError):
                    screenshot.capture(Path(tmp) / "shot.jpg", method="auto")

    def test_command_method(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "shot.jpg"
            result = screenshot.capture(
                dest,
                method="command",
                command="/bin/sh -c 'touch {dest}'",
            )
            self.assertEqual(result, dest)
            self.assertTrue(dest.exists())


if __name__ == "__main__":
    unittest.main()
