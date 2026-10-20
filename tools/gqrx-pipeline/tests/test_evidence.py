import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
import tempfile
import unittest
import wave
from pathlib import Path

import evidence
import session as session_mod


class FakeClient:
    def __init__(self, audio_dir: Path, iq_dir: Path):
        self.audio_dir = audio_dir
        self.iq_dir = iq_dir
        self.audio_count = 0
        self.iq_count = 0

    def audio_record(self, enabled):
        if not enabled:
            return
        self.audio_count += 1
        path = self.audio_dir / f"audio-{self.audio_count}.wav"
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(b"\x00\x00" * 16)

    def iq_record(self, enabled):
        if not enabled:
            return
        self.iq_count += 1
        path = self.iq_dir / f"iq-{self.iq_count}.raw"
        path.write_bytes(b"\x00" * 2000)


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.audio_dir = self.root / "audio"
        self.iq_dir = self.root / "iq"
        self.audio_dir.mkdir()
        self.iq_dir.mkdir()
        self.sess = session_mod.Session.create(self.root / "sessions", quota_bytes=100_000)
        self.client = FakeClient(self.audio_dir, self.iq_dir)

    def tearDown(self):
        self.tmp.cleanup()

    def test_audio_bundle_without_iq(self):
        cfg = evidence.EvidenceConfig(
            screenshot=False,
            audio=True,
            audio_seconds=0.01,
            audio_dir=self.audio_dir,
            spectrogram=False,
            iq=False,
        )
        bundle = evidence.capture_evidence(
            self.client,
            self.sess,
            cfg,
            reason="test",
            frequency_hz=100000000,
            mode="FM",
            passband_hz=15000,
            level_db=-40.0,
            squelch_db=-50.0,
        )
        metadata = json.loads((bundle / "metadata.json").read_text())
        self.assertEqual(metadata["mode"], "FM")
        self.assertIn("audio", metadata["artifacts"])
        self.assertNotIn("iq", metadata["artifacts"])
        self.assertTrue((bundle / "audio.wav").exists())
        index = self.sess.index_path.read_text()
        self.assertIn('"kind": "bundle"', index)
        self.assertIn('"iq_bytes": 0', index)

    def test_iq_is_bounded_and_optional(self):
        cfg = evidence.EvidenceConfig(
            screenshot=False,
            audio=False,
            spectrogram=False,
            iq=True,
            iq_seconds=1.0,
            iq_max_bytes=1000,
            iq_dir=self.iq_dir,
            iq_bytes_per_sample=8,
            iq_rate_hz=1000,
        )
        bundle = evidence.capture_evidence(
            self.client,
            self.sess,
            cfg,
            reason="test",
            frequency_hz=100000000,
        )
        metadata = json.loads((bundle / "metadata.json").read_text())
        iq = bundle / "iq.raw"
        self.assertTrue(iq.exists())
        self.assertEqual(iq.stat().st_size, 1000)
        self.assertTrue(any("truncated" in error for error in metadata["errors"]))
        self.assertIn('"iq_bytes": 1000', self.sess.index_path.read_text())

    def test_no_iq_when_rate_missing(self):
        cfg = evidence.EvidenceConfig(
            screenshot=False,
            audio=False,
            spectrogram=False,
            iq=True,
            iq_seconds=1.0,
            iq_dir=self.iq_dir,
        )
        bundle = evidence.capture_evidence(
            self.client,
            self.sess,
            cfg,
            reason="test",
            frequency_hz=100000000,
        )
        metadata = json.loads((bundle / "metadata.json").read_text())
        self.assertIn("sample rate is required", " ".join(metadata["errors"]))
        self.assertFalse((bundle / "iq.raw").exists())


if __name__ == "__main__":
    unittest.main()
