import unittest

import scan


class FakeClient:
    def __init__(self, fft=True):
        self.fft = fft
        self.frequencies = []
        self.commands = []

    def command(self, line, reply_lines=1):
        self.commands.append(line)
        if line == "FFT Q":
            if not self.fft:
                return ["RPRT 1"]
            return ["RPRT 0 F:433000000 R:2000000 Z:1024 S:432000000 E:434000000 B:1953.125 N:1024 C:1024"]
        if line.startswith("FFT V"):
            return ["RPRT 0 F:434000000 S:433000000 E:434000000 B:25000 C:4 -80 -40 -70 -75"]
        return ["RPRT 1"]

    def set_frequency(self, hz):
        self.frequencies.append(hz)

    def set_mode(self, mode, passband=None):
        pass

    def get_strength(self):
        return -50.0


class ScanTest(unittest.TestCase):
    def test_parse_fft_line(self):
        header, values = scan.parse_fft_line(
            "RPRT 0 F:433000000 R:2000000 Z:1024 S:432000000 E:434000000 B:1953.125 N:1024 C:4 -80 -40 -70 -75"
        )
        self.assertEqual(header["R"], 2000000)
        self.assertEqual(header["C"], 4)
        self.assertEqual(values, [-80.0, -40.0, -70.0, -75.0])

    def test_find_peaks(self):
        peaks = scan.find_peaks(
            [-80, -80, -40, -80, -80, -60, -80],
            start_hz=1000,
            bin_width_hz=10,
            min_prominence_db=6,
        )
        self.assertEqual([peak["freq_hz"] for peak in peaks], [1020, 1050])

    def test_fft_scan(self):
        client = FakeClient(fft=True)
        records = scan.fft_scan(
            client,
            start_hz=433000000,
            end_hz=435000000,
            pool_hz=25000,
            min_prominence_db=6,
            dwell_s=0,
        )
        self.assertTrue(records)
        self.assertTrue(all(433000000 <= record["freq_hz"] <= 435000000 for record in records))
        self.assertTrue(client.frequencies)

    def test_fft_unavailable(self):
        with self.assertRaises(scan.FFTUnavailable):
            scan.fft_scan(FakeClient(fft=False), start_hz=100, end_hz=200)

    def test_poll_scan(self):
        client = FakeClient()
        records = scan.poll_scan(client, start_hz=100, end_hz=200, step_hz=50, dwell_s=0)
        self.assertEqual([record["freq_hz"] for record in records], [100, 150, 200])
        self.assertTrue(all(record["level_db"] == -50.0 for record in records))


if __name__ == "__main__":
    unittest.main()
