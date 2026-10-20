#!/usr/bin/env python3
"""Bounded continuous recording through gqrx's remote recorder.

gqrx master cannot split a recording by itself and cannot accept a filename
over the remote protocol. This tool rotates the existing recorder at a fixed
interval, moves each completed file into the session, and records its size and
time range in ``index.jsonl``.

Rotation is intentionally coarse (seconds) because gqrx filenames have
one-second resolution and the sink appends on a collision. The short gap
between segments is the price of using the unmodified remote API; the native
recorder (P2) removes it.
"""

from __future__ import annotations

import argparse
import shutil
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

import session as session_mod
from gqrxctl import GqrxClient, GqrxError


IQ_SUFFIXES = (".raw", ".sigmf-data", ".cu8", ".cs16")
AUDIO_SUFFIXES = (".wav",)


class RotationError(RuntimeError):
    pass


@dataclass
class RotationConfig:
    mode: str = "iq"  # iq | audio
    segment_seconds: float = 60.0
    total_seconds: float = 0.0
    quota_bytes: int = 1024 ** 3
    retention_days: float = 7.0
    source_dir: Path = Path.home()
    rate_hz: Optional[float] = None
    freq_hz: Optional[int] = None
    bytes_per_sample: int = 8
    demod: Optional[str] = None
    passband_hz: Optional[int] = None
    min_start_gap_s: float = 1.1
    wait_timeout_s: float = 10.0


def suffixes_for(mode: str) -> tuple[str, ...]:
    return AUDIO_SUFFIXES if mode == "audio" else IQ_SUFFIXES


def estimate_segment_bytes(config: RotationConfig) -> int:
    if config.mode == "audio":
        # gqrx writes 48 kHz, stereo, 16-bit PCM WAV.
        return int(48000 * 2 * 2 * max(0.0, config.segment_seconds))
    if not config.rate_hz or config.rate_hz <= 0:
        raise RotationError("--rate is required for IQ rotation")
    return int(config.rate_hz * config.bytes_per_sample * max(0.0, config.segment_seconds))


def snapshot(directory: Path, mode: str) -> set[Path]:
    if not directory.exists():
        return set()
    suffixes = suffixes_for(mode)
    return {
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in suffixes
    }


def newest_new(directory: Path, before: set[Path], mode: str) -> Optional[Path]:
    if not directory.exists():
        return None
    suffixes = suffixes_for(mode)
    candidates = [
        path
        for path in directory.iterdir()
        if path.is_file()
        and path.suffix.lower() in suffixes
        and path not in before
        and path.stat().st_size > 0
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def wait_new(
    directory: Path,
    before: set[Path],
    mode: str,
    timeout: float,
) -> Optional[Path]:
    deadline = time.monotonic() + timeout
    while True:
        found = newest_new(directory, before, mode)
        if found is not None:
            return found
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.05)


def move_artifact(source: Path, destination_dir: Path) -> tuple[Path, Optional[Path]]:
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / source.name
    if destination.exists():
        stem = "".join(source.suffixes)
        destination = destination_dir / f"{source.name[: -len(stem)]}-{int(time.time() * 1000)}{stem}"
    shutil.move(str(source), str(destination))
    meta = None
    if source.name.endswith(".sigmf-data"):
        source_meta = source.with_name(source.name[: -len(".sigmf-data")] + ".sigmf-meta")
        if source_meta.exists():
            meta = destination_dir / source_meta.name
            shutil.move(str(source_meta), str(meta))
    return destination, meta


def run_rotation(
    client: GqrxClient,
    session: session_mod.Session,
    config: RotationConfig,
    *,
    stop_event=None,
) -> int:
    if config.mode not in ("iq", "audio"):
        raise RotationError("mode must be iq or audio")
    if config.segment_seconds <= 0:
        raise RotationError("segment_seconds must be positive")
    if config.mode == "iq" and (not config.rate_hz or config.rate_hz <= 0):
        raise RotationError("IQ rotation requires rate_hz")
    if not config.source_dir.exists():
        raise RotationError(f"source_dir does not exist: {config.source_dir}")

    if config.demod:
        client.set_mode(config.demod, config.passband_hz)
    if config.freq_hz:
        client.set_frequency(config.freq_hz)

    target_dir = session.path / ("iq" if config.mode == "iq" else "audio")
    target_dir.mkdir(parents=True, exist_ok=True)
    deadline = None if config.total_seconds <= 0 else time.monotonic() + config.total_seconds
    segments = 0
    last_start = 0.0

    while True:
        if stop_event is not None and stop_event.is_set():
            break
        if deadline is not None and time.monotonic() >= deadline:
            break

        if last_start and time.monotonic() - last_start < config.min_start_gap_s:
            time.sleep(config.min_start_gap_s - (time.monotonic() - last_start))

        estimated = estimate_segment_bytes(config)
        try:
            session.ensure_capacity(estimated)
        except session_mod.QuotaExceeded as exc:
            print(f"stop: {exc}", file=sys.stderr)
            break

        before = snapshot(config.source_dir, config.mode)
        started = datetime.now(timezone.utc)
        try:
            if config.mode == "audio":
                client.audio_record(True)
            else:
                client.iq_record(True)
            time.sleep(config.segment_seconds)
        finally:
            try:
                if config.mode == "audio":
                    client.audio_record(False)
                else:
                    client.iq_record(False)
            except GqrxError as exc:
                print(f"warning: stop recorder: {exc}", file=sys.stderr)

        source = wait_new(config.source_dir, before, config.mode, config.wait_timeout_s)
        if source is None:
            print(f"warning: no new {config.mode} file in {config.source_dir}", file=sys.stderr)
            last_start = time.monotonic()
            continue

        destination, meta = move_artifact(source, target_dir)
        size = destination.stat().st_size
        record = {
            "kind": "rotation",
            "file": str(destination.relative_to(session.path)),
            "meta": str(meta.relative_to(session.path)) if meta else None,
            "start": started.isoformat(),
            "end": datetime.now(timezone.utc).isoformat(),
            "mode": config.mode,
            "bytes": size,
            "rate_hz": config.rate_hz,
            "freq_hz": config.freq_hz,
            "format": config.mode,
        }
        session.record_bundle(destination.parent, record)
        segments += 1
        last_start = time.monotonic()
        if session.total_bytes() > session.quota_bytes:
            print("stop: session quota exceeded after segment", file=sys.stderr)
            break
        if estimated and size > estimated * 1.5:
            print(
                f"warning: segment {destination.name} is {size} bytes "
                f"(estimated {estimated}); update --rate/--bytes-per-sample",
                file=sys.stderr,
            )
    return segments


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7356)
    parser.add_argument("--mode", choices=["iq", "audio"], default="iq")
    parser.add_argument("--segment-seconds", type=float, default=60.0)
    parser.add_argument("--total-seconds", type=float, default=0.0,
                        help="0 = until stopped or quota reached")
    parser.add_argument("--quota-gb", type=float, default=1.0)
    parser.add_argument("--retention-days", type=float, default=7.0)
    parser.add_argument("--session-root", type=Path, default=Path("~/sdr/sessions"))
    parser.add_argument("--source-dir", type=Path, required=True,
                        help="gqrx [baseband] rec_dir or [audio] rec_dir")
    parser.add_argument("--rate", type=float, help="IQ rate (Hz)")
    parser.add_argument("--freq", type=int, help="frequency (Hz)")
    parser.add_argument("--bytes-per-sample", type=int, default=8,
                        help="8=CF32, 2=CU8, 4=CS16")
    parser.add_argument("--demod", help="optional demod mode, e.g. AM/FM")
    parser.add_argument("--passband", type=int)
    parser.add_argument("--min-start-gap", type=float, default=1.1)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    session = session_mod.Session.create(
        args.session_root,
        quota_bytes=int(args.quota_gb * 1024 ** 3),
        retention_days=args.retention_days,
        metadata={
            "mode": args.mode,
            "source_dir": str(args.source_dir),
            "rate_hz": args.rate,
            "freq_hz": args.freq,
            "segment_seconds": args.segment_seconds,
        },
    )
    config = RotationConfig(
        mode=args.mode,
        segment_seconds=args.segment_seconds,
        total_seconds=args.total_seconds,
        quota_bytes=session.quota_bytes,
        retention_days=args.retention_days,
        source_dir=Path(args.source_dir).expanduser(),
        rate_hz=args.rate,
        freq_hz=args.freq,
        bytes_per_sample=args.bytes_per_sample,
        demod=args.demod,
        passband_hz=args.passband,
        min_start_gap_s=args.min_start_gap,
    )
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
            segments = run_rotation(client, session, config, stop_event=stop_event)
    except (GqrxError, RotationError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"session: {session.path} segments={segments}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
