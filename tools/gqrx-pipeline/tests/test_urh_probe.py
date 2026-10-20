import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tempfile
import unittest

import numpy as np

import urh_probe


class UrhProbeTest(unittest.TestCase):
    def test_detect_format(self):
        self.assertEqual(urh_probe.detect_format(Path("x.raw")), "cf32")
        self.assertEqual(urh_probe.detect_format(Path("x.iq")), "cu8")
        self.assertEqual(urh_probe.detect_format(Path("x.cs16")), "cs16")
        self.assertEqual(urh_probe.detect_format(Path("x.wav")), "wav")

    def test_read_cf32_and_cu8(self):
        with tempfile.TemporaryDirectory() as tmp:
            cf32 = Path(tmp) / "x.raw"
            samples = (np.ones(100, dtype=np.complex64) * (0.5 + 0.25j))
            samples.tofile(cf32)
            loaded = urh_probe.read_iq(cf32, "cf32")
            self.assertEqual(len(loaded), 100)
            self.assertAlmostEqual(float(loaded[0].real), 0.5, places=5)
            cu8 = Path(tmp) / "x.iq"
            interleaved = np.array([127, 255, 0, 127] * 10, dtype=np.uint8)
            interleaved.tofile(cu8)
            loaded = urh_probe.read_iq(cu8, "cu8")
            self.assertEqual(len(loaded), 20)
            self.assertAlmostEqual(float(loaded[1].imag), -0.0039, places=2)

    def test_signal_window_is_bounded(self):
        samples = np.zeros(10000, dtype=np.complex64)
        samples[4000:4500] = 0.5 + 0.5j
        window = urh_probe.signal_window(samples, 1024)
        self.assertEqual(len(window), 1024)

    def test_flex_candidates(self):
        est = {"modulation_type": "ASK", "bit_length": 800}
        candidates = urh_probe.flex_candidates(est, 1_800_000)
        self.assertEqual(candidates[0]["m"], "OOK_PWM")
        self.assertAlmostEqual(candidates[0]["s"], 444)
        self.assertGreater(candidates[0]["l"], candidates[0]["s"])

    def test_build_proposal(self):
        est = {"modulation_type": "ASK", "bit_length": 800, "center": 0.1,
               "tolerance": 3, "noise": 0.09}
        proposal = urh_probe.build_proposal(Path("x.raw"), 1_800_000,
                                            np.zeros(100, dtype=np.complex64), est)
        self.assertEqual(proposal["modulation"], "ASK")
        self.assertTrue(proposal["flex_candidates"])
        self.assertIn("decision threshold", " ".join(proposal["notes"]))


if __name__ == "__main__":
    unittest.main()
