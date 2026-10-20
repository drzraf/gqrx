import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import decode


class DecodeTest(unittest.TestCase):
    def test_detect_format(self):
        self.assertEqual(decode.detect_format(Path("x.sigmf-data")), "cf32")
        self.assertEqual(decode.detect_format(Path("x.raw")), "cf32")
        self.assertEqual(decode.detect_format(Path("x.iq")), "cu8")
        self.assertEqual(decode.detect_format(Path("x.cs16")), "cs16")
        with self.assertRaises(decode.DecodeError):
            decode.detect_format(Path("x.wav"))

    def test_build_command(self):
        command = decode.build_command(
            "rtl433",
            Path("capture.raw"),
            rate_hz=250000,
            frequency_hz=433900000,
            fmt="cf32",
            flex="n=x,m=OOK_PWM,s=1,l=2",
            output=Path("events.jsonl"),
        )
        self.assertEqual(command[:6], ["rtl433", "-r", "cf32:capture.raw", "-s", "250000", "-f"])
        self.assertIn("433900000", command)
        self.assertIn("-X", command)
        self.assertIn("n=x,m=OOK_PWM,s=1,l=2", command)
        self.assertIn("-M", command)
        self.assertIn("json:events.jsonl", command)

    def test_dry_run_does_not_require_rtl433(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "capture.raw"
            path.write_bytes(b"\x00" * 16)
            with contextlib.redirect_stdout(io.StringIO()):
                command = decode.decode_file(
                    "rtl433",
                    path,
                    rate_hz=1000,
                    frequency_hz=100000000,
                    dry_run=True,
                )
            self.assertEqual(command[2], f"cf32:{path}")


if __name__ == "__main__":
    unittest.main()
