#!/usr/bin/env python3
"""Run URH-NG's auto-interpretation on a bounded IQ window.

This is an offline proposal generator, not a decoder. It reports the URH
modulation/timing estimate and a small set of flex-decoder candidates that a
human or script must validate against a known payload. The window selection
mirrors the RFAnalyzer harness: it prefers burst-dense regions rather than the
highest raw-energy region.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import wave
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np

DEFAULT_WINDOW = 1 << 18


class ProbeError(RuntimeError):
    pass


def detect_format(path: Path, explicit: Optional[str] = None) -> str:
    if explicit and explicit != "auto":
        return explicit
    name = path.name.lower()
    if name.endswith(".sigmf-data") or name.endswith(".raw") or name.endswith(".complex"):
        return "cf32"
    if name.endswith(".iq") or name.endswith(".cu8"):
        return "cu8"
    if name.endswith(".cs16"):
        return "cs16"
    if name.endswith(".wav"):
        return "wav"
    raise ProbeError(f"cannot infer format for {path}; pass --format")


def read_iq(path: Path, fmt: str) -> np.ndarray:
    if fmt == "wav":
        with wave.open(str(path), "rb") as handle:
            if handle.getsampwidth() != 2 or handle.getnchannels() != 2:
                raise ProbeError("WAV must be 16-bit stereo IQ")
            raw = handle.readframes(handle.getnframes())
        interleaved = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        return (interleaved[0::2] + 1j * interleaved[1::2]).astype(np.complex64)
    if fmt == "cf32":
        return np.fromfile(path, dtype="<c8").astype(np.complex64)
    if fmt == "cs16":
        interleaved = np.fromfile(path, dtype="<i2").astype(np.float32) / 32768.0
    elif fmt == "cu8":
        interleaved = (np.fromfile(path, dtype=np.uint8).astype(np.float32) - 127.5) / 127.5
    else:
        raise ProbeError(f"unsupported format: {fmt}")
    return (interleaved[0::2] + 1j * interleaved[1::2]).astype(np.complex64)


def signal_window(samples: np.ndarray, length: int) -> np.ndarray:
    """Prefer the burst-densest window (same idea as the RFAnalyzer harness)."""
    if length <= 0 or len(samples) <= length:
        return samples
    block = 256
    blocks = len(samples) // block
    window_blocks = length // block
    if blocks < 4 or window_blocks < 4 or window_blocks >= blocks:
        return samples[:length]
    magnitude = np.abs(samples[:blocks * block]).reshape(blocks, block).mean(axis=1)
    step = max(1, window_blocks // 2)
    best_start, best_score = 0, -1.0
    for start in range(0, blocks - window_blocks + 1, step):
        chunk = magnitude[start:start + window_blocks]
        mean = float(chunk.mean())
        if mean <= 1e-9:
            continue
        score = float(np.abs(np.diff(chunk)).sum()) / max(1, len(chunk) - 1) / mean
        if score > best_score:
            best_score, best_start = score, start
    return samples[best_start * block:best_start * block + length]


def flex_candidates(estimate: Dict[str, Any], sample_rate: int) -> List[Dict[str, Any]]:
    bit_samples = int(estimate.get("bit_length") or 0)
    if bit_samples <= 0 or sample_rate <= 0:
        return []
    bit_us = bit_samples * 1e6 / sample_rate
    short = max(1, int(round(bit_us)))
    long = max(short + 1, int(round(bit_us * 2)))
    modulation = str(estimate.get("modulation_type") or "").upper()
    if modulation in ("ASK", "OOK"):
        return [
            {"m": "OOK_PWM", "s": short, "l": long, "r": max(2000, long * 2),
             "g": 0, "t": 0, "note": "validate short/long against pulses; add bits= for stable frames"},
            {"m": "OOK_PCM", "s": short, "l": short, "r": max(2000, short * 2),
             "g": 0, "t": 0, "note": "try if PWM does not frame"},
        ]
    if modulation == "FSK":
        return [{"m": "FSK_PCM", "s": short, "l": short, "note": "rtl_433 flex FSK candidate"}]
    if modulation == "PSK":
        return [{"m": "PSK_PCM", "s": short, "l": short, "note": "rtl_433 flex PSK candidate"}]
    return []


def build_proposal(path: Path, sample_rate: int, window: np.ndarray,
                   estimate: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if estimate is None:
        return {
            "source": str(path),
            "sample_rate_hz": sample_rate,
            "window_samples": int(len(window)),
            "modulation": None,
            "flex_candidates": [],
            "next_steps": ["Open the IQ in URH-NG and adjust the noise/segmentation manually."],
        }
    bit_samples = int(estimate.get("bit_length") or 0)
    proposal = {
        "source": str(path),
        "sample_rate_hz": sample_rate,
        "window_samples": int(len(window)),
        "modulation": estimate.get("modulation_type"),
        "bit_length_samples": bit_samples,
        "bit_length_us": round(bit_samples * 1e6 / sample_rate, 3) if sample_rate else None,
        "center": estimate.get("center"),
        "tolerance": estimate.get("tolerance"),
        "noise": estimate.get("noise"),
        "flex_candidates": flex_candidates(estimate, sample_rate),
        "notes": [
            "center is URH's decision threshold, not a carrier offset",
            "bit_length_us is the base symbol, not necessarily the short/long PWM widths",
            "validate every candidate by decoding a known payload; add bits= to suppress fragments",
        ],
        "next_steps": [
            "Open the file in URH-NG, set the true sample rate, and run Autodetect parameters",
            "Use Analyze -> Auto-identify protocol (PHZ DB) if the PHZ database may contain it",
            "Use Assign participants/fields or the Crypto Toolkit for unknown/rolling-code devices",
        ],
    }
    return proposal


def run_urh(window: np.ndarray) -> Optional[Dict[str, Any]]:
    try:
        from urh.ainterpretation import AutoInterpretation
        from urh.signalprocessing.IQArray import IQArray
    except Exception as exc:  # noqa: BLE001
        raise ProbeError(f"URH import failed ({exc}); install urh-ng") from exc
    return AutoInterpretation.estimate(IQArray(window))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument("--sample-rate", type=int, required=True)
    parser.add_argument("--format", choices=["auto", "cf32", "cu8", "cs16", "wav"], default="auto")
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW,
                        help="max complex samples analysed (default 2^18)")
    parser.add_argument("--no-window-search", action="store_true")
    parser.add_argument("--output", type=Path, help="write proposal JSON")
    parser.add_argument("--symlink-complex", action="store_true",
                        help="create a .complex symlink for the URH GUI")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        fmt = detect_format(args.file, args.format)
        samples = read_iq(args.file, fmt)
        if samples.size == 0:
            raise ProbeError("no samples read")
        window = samples[:args.window] if args.no_window_search else signal_window(samples, args.window)
        estimate = run_urh(window)
        proposal = build_proposal(args.file, args.sample_rate, window, estimate)
    except (ProbeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    text = json.dumps(proposal, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    if args.symlink_complex and fmt == "cf32":
        link = args.file.with_suffix(".complex")
        if not link.exists():
            os.symlink(args.file.resolve(), link)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
