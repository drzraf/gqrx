#!/usr/bin/env python3
"""Adaptive level monitor that produces bounded evidence bundles.

This is deliberately generic: frequency, mode, passband and squelch are
parameters. It works for voice, tones, FSK, OOK and unknown bursts. The
expensive work (decode/URH) is not done here; only a screenshot/audio/
spectrogram bundle is produced, plus an optional bounded IQ segment.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, Optional

import evidence as evidence_mod
import session as session_mod
from gqrxctl import GqrxClient, GqrxError


def percentile(values, fraction: float) -> float:
    if not values:
        return float("-inf")
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return ordered[index]


@dataclass
class MonitorConfig:
    interval_s: float = 0.1
    warmup_s: float = 5.0
    window: int = 100
    noise_fraction: float = 0.8
    margin_db: float = 6.0
    cooldown_s: float = 2.0
    record_interval_s: float = 1.0
    min_level_db: Optional[float] = None


class LevelTracker:
    def __init__(self, config: MonitorConfig):
        self.config = config
        self.levels: Deque[float] = deque(maxlen=max(10, config.window))
        self.started = time.monotonic()
        self.last_trigger = 0.0

    def noise_floor(self) -> float:
        return percentile(self.levels, self.config.noise_fraction)

    def ready(self) -> bool:
        return (
            len(self.levels) >= max(5, self.config.window // 4)
            and time.monotonic() - self.started >= self.config.warmup_s
        )

    def threshold(self, squelch_db: float) -> float:
        noise = self.noise_floor()
        adaptive = noise + self.config.margin_db
        if squelch_db <= -149.0:
            threshold = adaptive
        else:
            threshold = max(squelch_db, adaptive)
        if self.config.min_level_db is not None:
            threshold = max(threshold, self.config.min_level_db)
        return threshold

    def update(self, level_db: float) -> Optional[float]:
        self.levels.append(level_db)
        if not self.ready():
            return None
        return self.noise_floor()

    def trigger_allowed(self, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        return now - self.last_trigger >= self.config.cooldown_s

    def mark_trigger(self, now: Optional[float] = None) -> None:
        self.last_trigger = time.monotonic() if now is None else now


def run_monitor(
    client: GqrxClient,
    sess: session_mod.Session,
    evidence_config: evidence_mod.EvidenceConfig,
    monitor_config: MonitorConfig,
    *,
    frequency_hz: int,
    mode: Optional[str] = None,
    passband_hz: Optional[int] = None,
    duration_s: Optional[float] = None,
    stop_event=None,
) -> int:
    client.set_frequency(frequency_hz)
    if mode:
        client.set_mode(mode, passband_hz)
    tracker = LevelTracker(monitor_config)
    deadline = None if duration_s is None else time.monotonic() + max(0.0, duration_s)
    triggers = 0
    last_record = 0.0
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            if deadline is not None and time.monotonic() >= deadline:
                break
            try:
                level = client.get_strength()
                squelch = client.get_squelch()
            except GqrxError as exc:
                print(f"warning: {exc}", file=sys.stderr)
                time.sleep(max(0.5, monitor_config.interval_s))
                continue
            noise = tracker.update(level)
            threshold = tracker.threshold(squelch) if noise is not None else None
            now = time.monotonic()
            if now - last_record >= monitor_config.record_interval_s:
                last_record = now
                record = {
                    "kind": "level",
                    "freq_hz": frequency_hz,
                    "level_db": level,
                    "squelch_db": squelch,
                    "noise_db": noise,
                    "threshold_db": threshold,
                }
                sess.record_scan(record)
            if (
                threshold is not None
                and level >= threshold
                and tracker.trigger_allowed()
            ):
                tracker.mark_trigger()
                triggers += 1
                try:
                    bundle = evidence_mod.capture_evidence(
                        client,
                        sess,
                        evidence_config,
                        reason="level",
                        frequency_hz=frequency_hz,
                        mode=mode,
                        passband_hz=passband_hz,
                        level_db=level,
                        squelch_db=squelch,
                    )
                    print(f"evidence: {bundle}", file=sys.stderr)
                except (session_mod.QuotaExceeded, GqrxError, OSError) as exc:
                    print(f"warning: evidence capture failed: {exc}", file=sys.stderr)
            time.sleep(max(0.01, monitor_config.interval_s))
    except KeyboardInterrupt:
        pass
    return triggers


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7356)
    parser.add_argument("--freq", type=int, required=True, help="frequency (Hz)")
    parser.add_argument("--mode", help="demod mode, e.g. AM/FM/USB")
    parser.add_argument("--passband", type=int, help="passband (Hz)")
    parser.add_argument("--duration", type=float, help="run time (s); default infinite")
    parser.add_argument("--session-root", type=Path, default=Path("~/sdr/sessions"))
    parser.add_argument("--quota-gb", type=float, default=1.0)
    parser.add_argument("--retention-days", type=float, default=7.0)

    parser.add_argument("--no-screenshot", action="store_true")
    parser.add_argument("--screenshot-method", default="auto",
                        choices=["auto", "wrapper", "grim", "x11", "command"])
    parser.add_argument("--screenshot-pidfile", type=Path)
    parser.add_argument("--screenshot-dir", type=Path)
    parser.add_argument("--screenshot-signal", default="RTMIN")
    parser.add_argument("--screenshot-command")

    parser.add_argument("--no-audio", action="store_true")
    parser.add_argument("--audio-seconds", type=float, default=2.0)
    parser.add_argument("--audio-dir", type=Path)

    parser.add_argument("--no-spectrogram", action="store_true")

    parser.add_argument("--iq-seconds", type=float, default=0.0,
                        help="bounded IQ capture per trigger; 0 disables it")
    parser.add_argument("--iq-max-mb", type=float, default=8.0)
    parser.add_argument("--iq-rate", type=float, help="IQ rate (Hz) for budgeting")
    parser.add_argument("--iq-dir", type=Path)
    parser.add_argument("--iq-bytes-per-sample", type=int, default=8,
                        help="8=CF32, 2=CU8, 4=CS16")
    return parser


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    sess = session_mod.Session.create(
        args.session_root,
        quota_bytes=int(args.quota_gb * 1024 ** 3),
        retention_days=args.retention_days,
        metadata={
            "host": args.host,
            "port": args.port,
            "freq_hz": args.freq,
            "mode": args.mode,
            "passband_hz": args.passband,
        },
    )
    evidence_config = evidence_mod.EvidenceConfig(
        screenshot=not args.no_screenshot,
        screenshot_method=args.screenshot_method,
        screenshot_pidfile=args.screenshot_pidfile,
        screenshot_dir=args.screenshot_dir,
        screenshot_signal=args.screenshot_signal,
        screenshot_command=args.screenshot_command,
        audio=not args.no_audio,
        audio_seconds=args.audio_seconds,
        audio_dir=args.audio_dir,
        spectrogram=not args.no_spectrogram,
        iq=args.iq_seconds > 0,
        iq_seconds=args.iq_seconds,
        iq_max_bytes=int(args.iq_max_mb * 1024 ** 2),
        iq_dir=args.iq_dir,
        iq_bytes_per_sample=args.iq_bytes_per_sample,
        iq_rate_hz=args.iq_rate,
    )
    monitor_config = MonitorConfig()
    stop_event = None
    try:
        import threading

        stop_event = threading.Event()

        def _stop(_signum, _frame):
            stop_event.set()

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
    except (ValueError, OSError):
        stop_event = None

    try:
        with GqrxClient(args.host, args.port) as client:
            triggers = run_monitor(
                client,
                sess,
                evidence_config,
                monitor_config,
                frequency_hz=args.freq,
                mode=args.mode,
                passband_hz=args.passband,
                duration_s=args.duration,
                stop_event=stop_event,
            )
    except (GqrxError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"session: {sess.path} triggers={triggers}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
