#!/usr/bin/env python3
"""CPU-friendly spectrum scanning through the gqrx remote protocol.

If PR #1458 (``FFT Q`` / ``FFT V``) is available, one query returns the whole
visible spectrum and the scanner peak-picks it. Otherwise it falls back to
per-frequency ``F`` + ``l STRENGTH`` polling, which is slower and coarser.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from gqrxctl import GqrxClient, GqrxError


class FFTUnavailable(RuntimeError):
    pass


def parse_fft_line(line: str) -> Tuple[Dict[str, float], List[float]]:
    if not line.startswith("RPRT 0"):
        raise FFTUnavailable(line or "empty FFT response")
    header: Dict[str, float] = {}
    values: List[float] = []
    for token in line.split()[2:]:
        if ":" in token:
            key, raw = token.split(":", 1)
            if key.isalpha() and len(key) <= 3:
                try:
                    header[key] = float(raw)
                except ValueError:
                    continue
                continue
        try:
            values.append(float(token))
        except ValueError:
            continue
    if "C" in header and values and int(header["C"]) != len(values):
        # Keep the values; some gqrx builds append extra diagnostics.
        header["C_actual"] = float(len(values))
    return header, values


def percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return float("-inf")
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return ordered[index]


def find_peaks(
    values: Sequence[float],
    *,
    start_hz: float,
    bin_width_hz: float,
    min_prominence_db: float = 6.0,
    max_peaks: int = 20,
) -> List[Dict[str, float]]:
    if not values or bin_width_hz <= 0:
        return []
    noise = percentile(values, 0.5)
    candidates: List[Dict[str, float]] = []
    for index, level in enumerate(values):
        left = values[index - 1] if index > 0 else level
        right = values[index + 1] if index + 1 < len(values) else level
        if level < left or level < right:
            continue
        prominence = level - noise
        if prominence < min_prominence_db:
            continue
        candidates.append(
            {
                "freq_hz": start_hz + index * bin_width_hz,
                "level_db": level,
                "prominence_db": prominence,
            }
        )
    candidates.sort(key=lambda item: item["prominence_db"], reverse=True)
    return candidates[: max(1, max_peaks)]


def fft_available(client: GqrxClient) -> bool:
    try:
        reply = client.command("FFT Q", 1)[0]
        return reply.startswith("RPRT 0")
    except GqrxError:
        return False


def fft_sweep(
    client: GqrxClient,
    *,
    start_hz: int,
    end_hz: int,
    pool_hz: float = 25_000.0,
    min_prominence_db: float = 6.0,
    max_peaks: int = 20,
    dwell_s: float = 0.1,
    sample_rate_hz: Optional[float] = None,
    include_spectrum: bool = False,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    if end_hz <= start_hz:
        raise ValueError("end_hz must be greater than start_hz")
    if not fft_available(client):
        raise FFTUnavailable("FFT Q returned an error; gqrx PR #1458 is not available")

    query = client.command("FFT Q", 1)[0]
    header, _ = parse_fft_line(query)
    span = header.get("R") or (header.get("N", 0) * header.get("B", 0))
    if sample_rate_hz:
        span = float(sample_rate_hz)
    if not span or span <= 0:
        raise FFTUnavailable("cannot determine FFT span; pass --sample-rate")
    step = max(1.0, span * 0.8)

    records: List[Dict[str, Any]] = []
    spectra: List[Dict[str, Any]] = []
    seen: List[float] = []
    center = float(start_hz) + span / 2.0
    while center - span / 2.0 < end_hz:
        client.set_frequency(int(round(center)))
        if dwell_s > 0:
            import time

            time.sleep(dwell_s)
        reply = client.command(f"FFT V W:{float(pool_hz):.1f}", 1)[0]
        fft_header, values = parse_fft_line(reply)
        bin_width = fft_header.get("B", pool_hz)
        visible_start = fft_header.get("S", center - span / 2.0)
        if include_spectrum:
            spectra.append(
                {
                    "center_hz": center,
                    "span_hz": span,
                    "start_hz": visible_start,
                    "bin_width_hz": bin_width,
                    "values": values,
                }
            )
        for peak in find_peaks(
            values,
            start_hz=visible_start,
            bin_width_hz=bin_width,
            min_prominence_db=min_prominence_db,
            max_peaks=max_peaks,
        ):
            if peak["freq_hz"] < start_hz or peak["freq_hz"] > end_hz:
                continue
            if any(abs(peak["freq_hz"] - previous) < max(1.0, bin_width / 2.0) for previous in seen):
                continue
            seen.append(peak["freq_hz"])
            peak.update(
                {
                    "center_hz": center,
                    "span_hz": span,
                    "bin_width_hz": bin_width,
                    "noise_db": percentile(values, 0.5),
                }
            )
            records.append(peak)
        center += step
    records.sort(key=lambda item: item["freq_hz"])
    return records, spectra


def fft_scan(
    client: GqrxClient,
    *,
    start_hz: int,
    end_hz: int,
    pool_hz: float = 25_000.0,
    min_prominence_db: float = 6.0,
    max_peaks: int = 20,
    dwell_s: float = 0.1,
    sample_rate_hz: Optional[float] = None,
) -> List[Dict[str, Any]]:
    records, _ = fft_sweep(
        client,
        start_hz=start_hz,
        end_hz=end_hz,
        pool_hz=pool_hz,
        min_prominence_db=min_prominence_db,
        max_peaks=max_peaks,
        dwell_s=dwell_s,
        sample_rate_hz=sample_rate_hz,
        include_spectrum=False,
    )
    return records

def poll_scan(
    client: GqrxClient,
    *,
    start_hz: int,
    end_hz: int,
    step_hz: int = 25_000,
    dwell_s: float = 0.15,
    mode: Optional[str] = None,
    passband_hz: Optional[int] = None,
) -> List[Dict[str, Any]]:
    if step_hz <= 0:
        raise ValueError("step_hz must be positive")
    if mode:
        client.set_mode(mode, passband_hz)
    records: List[Dict[str, Any]] = []
    freq = int(start_hz)
    while freq <= end_hz:
        client.set_frequency(freq)
        if dwell_s > 0:
            import time

            time.sleep(dwell_s)
        try:
            level = client.get_strength()
        except GqrxError as exc:
            records.append({"freq_hz": freq, "error": str(exc)})
        else:
            records.append({"freq_hz": freq, "level_db": level})
        freq += step_hz
    return records


def write_records(records: Iterable[Dict[str, Any]], path: Optional[Path]) -> None:
    if path is None:
        for record in records:
            print(json.dumps(record, sort_keys=True))
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7356)
    parser.add_argument("--start", type=int, required=True, help="start frequency (Hz)")
    parser.add_argument("--end", type=int, required=True, help="end frequency (Hz)")
    parser.add_argument("--mode", choices=["auto", "fft", "poll"], default="auto")
    parser.add_argument("--demod", help="demod mode for poll fallback, e.g. AM")
    parser.add_argument("--passband", type=int, help="passband for poll fallback (Hz)")
    parser.add_argument("--step", type=int, default=25_000, help="poll step (Hz)")
    parser.add_argument("--pool", type=float, default=25_000.0, help="FFT output bin width (Hz)")
    parser.add_argument("--min-prominence", type=float, default=6.0, help="peak prominence (dB)")
    parser.add_argument("--max-peaks", type=int, default=20)
    parser.add_argument("--dwell", type=float, default=0.1, help="settle time per center (s)")
    parser.add_argument("--sample-rate", type=float, help="FFT span (Hz) if FFT Q omits it")
    parser.add_argument("--output", type=Path, help="append JSONL here instead of stdout")
    args = parser.parse_args(argv)

    try:
        with GqrxClient(args.host, args.port) as client:
            if args.mode in ("auto", "fft"):
                try:
                    records = fft_scan(
                        client,
                        start_hz=args.start,
                        end_hz=args.end,
                        pool_hz=args.pool,
                        min_prominence_db=args.min_prominence,
                        max_peaks=args.max_peaks,
                        dwell_s=args.dwell,
                        sample_rate_hz=args.sample_rate,
                    )
                except FFTUnavailable as exc:
                    if args.mode == "fft":
                        raise
                    print(f"note: {exc}; falling back to poll scan", file=sys.stderr)
                    records = poll_scan(
                        client,
                        start_hz=args.start,
                        end_hz=args.end,
                        step_hz=args.step,
                        dwell_s=args.dwell,
                        mode=args.demod,
                        passband_hz=args.passband,
                    )
            else:
                records = poll_scan(
                    client,
                    start_hz=args.start,
                    end_hz=args.end,
                    step_hz=args.step,
                    dwell_s=args.dwell,
                    mode=args.demod,
                    passband_hz=args.passband,
                )
    except (GqrxError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    write_records(records, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
