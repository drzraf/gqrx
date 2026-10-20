import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tempfile
import unittest

import scan_session


class GridAccumulatorTest(unittest.TestCase):
    def test_max_hold_and_occupancy(self):
        grid = scan_session.GridAccumulator(1000, 1100, 10)
        grid.add(1000, 10, [-80, -40, -70, -75, -78])
        grid.add(1000, 10, [-80, -50, -70, -75, -78])
        self.assertAlmostEqual(grid.max_hold[1], -40)
        self.assertAlmostEqual(grid.occupancy_pct()[1], 100.0)
        peaks = grid.peaks(min_prominence_db=6, max_peaks=5)
        self.assertTrue(peaks)
        self.assertAlmostEqual(peaks[0]["freq_hz"], 1010, places=1)
        self.assertGreater(peaks[0]["occupancy_pct"], 0)

    def test_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            grid = scan_session.GridAccumulator(1000, 1020, 10)
            grid.add(1000, 10, [-80, -40])
            path = Path(tmp) / "heatmap.csv"
            grid.write_csv(path)
            self.assertIn("freq_hz,max_hold_db,occupancy_pct", path.read_text())

    def test_bookmarks_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bookmarks.csv"
            peaks = [{"freq_hz": 433900000.0}, {"freq_hz": 145500000.0}]
            self.assertEqual(scan_session.write_bookmarks(path, peaks), 2)
            text = path.read_text()
            self.assertIn("scan", text)
            self.assertIn("433900000", text)
            self.assertEqual(scan_session.write_bookmarks(path, peaks), 0)
            self.assertEqual(text.count("433900000"), 1)


class SweepTest(unittest.TestCase):
    def test_fft_sweep_returns_spectra(self):
        import scan

        class Fake:
            def command(self, line, reply_lines=1):
                if line == "FFT Q":
                    return ["RPRT 0 F:1000 R:100 S:950 E:1050 B:10 N:10 C:10"]
                if line.startswith("FFT V"):
                    return ["RPRT 0 F:1000 S:950 E:1050 B:10 C:5 -80 -40 -70 -75 -78"]
                return ["RPRT 1"]

            def set_frequency(self, hz):
                pass

        peaks, spectra = scan.fft_sweep(
            Fake(), start_hz=900, end_hz=1100, pool_hz=10, dwell_s=0,
            include_spectrum=True,
        )
        self.assertTrue(spectra)
        self.assertTrue(peaks)
        self.assertEqual(len(spectra[0]["values"]), 5)


if __name__ == "__main__":
    unittest.main()
