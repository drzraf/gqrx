import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import session as session_mod


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_and_index(self):
        sess = session_mod.Session.create(self.root, quota_bytes=1024, metadata={"mode": "AM"})
        bundle = sess.bundle_dir(433900000)
        (bundle / "metadata.json").write_text('{"ok": true}\n')
        sess.record_bundle(bundle, {"kind": "bundle", "freq_hz": 433900000})
        self.assertTrue(sess.session_path.exists())
        self.assertIn('"mode": "AM"', sess.session_path.read_text())
        self.assertIn('"kind": "bundle"', sess.index_path.read_text())
        self.assertGreater(sess.total_bytes(), 0)

    def test_bundle_dirs_are_unique(self):
        sess = session_mod.Session.create(self.root)
        moment = datetime(2026, 10, 20, 1, 2, 3, tzinfo=timezone.utc)
        first = sess.bundle_dir(100000000, moment)
        second = sess.bundle_dir(100000000, moment)
        self.assertNotEqual(first, second)

    def test_quota(self):
        sess = session_mod.Session.create(self.root, quota_bytes=1000)
        base = sess.total_bytes()
        sess.quota_bytes = base + 100
        (sess.path / "big.bin").write_bytes(b"x" * 80)
        sess.ensure_capacity(10)
        with self.assertRaises(session_mod.QuotaExceeded):
            sess.ensure_capacity(30)

    def test_plan_iq_capture_is_bounded(self):
        sess = session_mod.Session.create(self.root, quota_bytes=10_000)
        seconds, size = sess.plan_iq_capture(
            1000,
            bytes_per_sample=2,
            max_bytes=500,
            desired_seconds=10,
        )
        self.assertEqual(size, 500)
        self.assertAlmostEqual(seconds, 0.25)

    def test_plan_iq_zero_when_no_budget(self):
        sess = session_mod.Session.create(self.root, quota_bytes=10)
        (sess.path / "fill.bin").write_bytes(b"x" * 10)
        seconds, size = sess.plan_iq_capture(1000, max_bytes=100, desired_seconds=1)
        self.assertEqual((seconds, size), (0.0, 0))

    def test_cleanup_respects_retention_and_keep(self):
        sess = session_mod.Session.create(self.root, retention_days=1)
        old = sess.bundle_dir(100000000, datetime.now(timezone.utc) - timedelta(days=2))
        kept = sess.bundle_dir(100000001, datetime.now(timezone.utc) - timedelta(days=2))
        (kept / "KEEP").write_text("")
        old_time = time.time() - 2 * 86400
        os.utime(old, (old_time, old_time))
        os.utime(kept, (old_time, old_time))
        removed = sess.cleanup_expired()
        self.assertIn(old, removed)
        self.assertNotIn(kept, removed)
        self.assertFalse(old.exists())
        self.assertTrue(kept.exists())

    def test_copy_limited(self):
        src = self.root / "src.bin"
        dst = self.root / "dst.bin"
        src.write_bytes(b"0123456789")
        written = session_mod.copy_limited(src, dst, 4)
        self.assertEqual(written, 4)
        self.assertEqual(dst.read_bytes(), b"0123")


if __name__ == "__main__":
    unittest.main()
