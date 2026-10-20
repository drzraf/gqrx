import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tempfile
import unittest
from pathlib import Path

import rotate
import session as session_mod


class FakeClient:
    def __init__(self, source_dir, suffix=".raw"):
        self.source_dir = source_dir
        self.suffix = suffix
        self.count = 0
        self.records = []

    def iq_record(self, enabled):
        if enabled:
            self.count += 1
            (self.source_dir / f"gqrx_test_{self.count}_fc{self.suffix}").write_bytes(b"\0" * 800)

    def audio_record(self, enabled):
        if enabled:
            self.count += 1
            (self.source_dir / f"gqrx_test_{self.count}.wav").write_bytes(b"\0" * 800)


class RotateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_estimate(self):
        cfg = rotate.RotationConfig(mode="iq", segment_seconds=10, rate_hz=1000, bytes_per_sample=2)
        self.assertEqual(rotate.estimate_segment_bytes(cfg), 20000)
        cfg = rotate.RotationConfig(mode="audio", segment_seconds=10)
        self.assertEqual(rotate.estimate_segment_bytes(cfg), 1920000)
        with self.assertRaises(rotate.RotationError):
            rotate.estimate_segment_bytes(rotate.RotationConfig(mode="iq", rate_hz=None))

    def test_move_sigmf_moves_meta(self):
        data = self.source / "a.sigmf-data"
        meta = self.source / "a.sigmf-meta"
        data.write_bytes(b"data")
        meta.write_text("meta")
        dest_dir = self.root / "dest"
        moved, moved_meta = rotate.move_artifact(data, dest_dir)
        self.assertEqual(moved.name, "a.sigmf-data")
        self.assertEqual(moved_meta.name, "a.sigmf-meta")
        self.assertTrue((dest_dir / "a.sigmf-data").exists())
        self.assertTrue((dest_dir / "a.sigmf-meta").exists())

    def test_rotation_creates_indexed_segments(self):
        sess = session_mod.Session.create(self.root / "sessions", quota_bytes=100_000)
        cfg = rotate.RotationConfig(
            mode="iq",
            segment_seconds=0.1,
            total_seconds=0.35,
            source_dir=self.source,
            rate_hz=1000,
            bytes_per_sample=8,
            min_start_gap_s=0,
            wait_timeout_s=1,
        )
        client = FakeClient(self.source)
        segments = rotate.run_rotation(client, sess, cfg)
        self.assertGreaterEqual(segments, 1)
        index = sess.index_path.read_text()
        self.assertIn('"kind": "rotation"', index)
        self.assertTrue(list((sess.path / "iq").iterdir()))

    def test_missing_source_dir(self):
        cfg = rotate.RotationConfig(
            mode="iq",
            source_dir=self.root / "missing",
            rate_hz=1000,
        )
        with self.assertRaises(rotate.RotationError):
            rotate.run_rotation(object(), session_mod.Session.create(self.root / "s"), cfg)


if __name__ == "__main__":
    unittest.main()
