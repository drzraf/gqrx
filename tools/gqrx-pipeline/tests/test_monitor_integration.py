import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tempfile
import unittest
from pathlib import Path

import evidence
import monitor
import session as session_mod


class FakeClient:
    def __init__(self):
        self.levels = [-80.0] * 10 + [-40.0] * 1000
        self.frequencies = []
        self.modes = []

    def set_frequency(self, hz):
        self.frequencies.append(hz)

    def set_mode(self, mode, passband=None):
        self.modes.append((mode, passband))

    def get_strength(self):
        return self.levels.pop(0) if len(self.levels) > 1 else self.levels[0]

    def get_squelch(self):
        return -150.0


class MonitorIntegrationTest(unittest.TestCase):
    def test_monitor_triggers_a_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = session_mod.Session.create(Path(tmp), quota_bytes=100_000)
            cfg = evidence.EvidenceConfig(
                screenshot=False,
                audio=False,
                spectrogram=False,
                iq=False,
            )
            monitor_cfg = monitor.MonitorConfig(
                interval_s=0.01,
                warmup_s=0,
                window=10,
                cooldown_s=0,
                record_interval_s=0,
            )
            client = FakeClient()
            triggers = monitor.run_monitor(
                client,
                sess,
                cfg,
                monitor_cfg,
                frequency_hz=145500000,
                mode="FM",
                passband_hz=15000,
                duration_s=0.15,
            )
            self.assertGreaterEqual(triggers, 1)
            self.assertTrue(any(sess.bundles_dir.iterdir()))
            self.assertIn('"kind": "bundle"', sess.index_path.read_text())
            self.assertEqual(client.frequencies[0], 145500000)
            self.assertEqual(client.modes[0], ("FM", 15000))


if __name__ == "__main__":
    unittest.main()
