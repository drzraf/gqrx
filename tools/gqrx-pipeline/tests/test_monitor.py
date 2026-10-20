import unittest

import monitor


class LevelTrackerTest(unittest.TestCase):
    def test_noise_threshold_and_cooldown(self):
        cfg = monitor.MonitorConfig(
            warmup_s=0,
            window=10,
            noise_fraction=0.8,
            margin_db=6,
            cooldown_s=2,
        )
        tracker = monitor.LevelTracker(cfg)
        for _ in range(10):
            tracker.update(-80.0)
        self.assertTrue(tracker.ready())
        self.assertAlmostEqual(tracker.noise_floor(), -80.0)
        self.assertAlmostEqual(tracker.threshold(-150.0), -74.0)
        self.assertAlmostEqual(tracker.threshold(-50.0), -50.0)
        self.assertTrue(tracker.trigger_allowed())
        tracker.mark_trigger()
        self.assertFalse(tracker.trigger_allowed())

    def test_not_ready_before_warmup(self):
        cfg = monitor.MonitorConfig(warmup_s=10, window=10)
        tracker = monitor.LevelTracker(cfg)
        for _ in range(10):
            tracker.update(-80.0)
        self.assertFalse(tracker.ready())


if __name__ == "__main__":
    unittest.main()
