#!/usr/bin/env python3
"""Repeat an FFT scan and accumulate a reviewable session.

This is the CPU-friendly P4 path: one FFT query per window, no per-frequency
retuning, no IQ storage. It writes JSONL peaks, a max-hold/occupancy heatmap,
a CSV summary, and optionally merges hits into gqrx's bookmarks.csv.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

import scan
import session as session_mod
from gqrxctl import GqrxClient, GqrxError


class GridAccumulator:
    def __init__(self, start_hz: int, end_hz: int, bin_width_hz: float):
        import numpy as np

        if end_hz <= start_hz:
            raise ValueError("end_hz must be greater than start_hz")
        if bin_width_hz <= 0:
            raise ValueError("bin_width_hz must be positive")
        self.np = np
        self.start_hz = float(start_hz)
        self.end_hz = float(end_hz)
        self.bin_width_hz = float(bin_width_hz)
        self.nbins = max(1, int(math.ceil((end_hz - start_hz) / bin_width_hz)))
        self.max_hold = np.full(self.nbins, -200.0, dtype=float)
        self._sum = np.zeros(self.nbins, dtype=float)
        self._count = np.zeros(self.nbins, dtype=int)
        self._above = np.zeros(self.nbins, dtype=int)
        self.sweeps = 0

    def add(self, start_hz: float, bin_width_hz: float, values: Sequence[float],
            margin_db: float = 6.0) -> None:
        np = self.np
        if not values or bin_width_hz <= 0:
            return
        freqs = float(start_hz) + np.arange(len(values), dtype=float) * bin_width_hz
        idx = np.floor((freqs - self.start_hz) / self.bin_width_hz).astype(int)
        valid = (idx >= 0) & (idx < self.nbins)
        if not valid.any():
            return
        idx = idx[valid]
        vals = np.asarray(values, dtype=float)[valid]
        np.maximum.at(self.max_hold, idx, vals)
        np.add.at(self._sum, idx, vals)
        np.add.at(self._count, idx, 1)
        noise = float(np.median(vals))
        above = vals >= noise + margin_db
        np.add.at(self._above, idx[above], 1)
        self.sweeps += 1

    def mean(self):
        np = self.np
        return np.divide(self._sum, self._count, out=np.full(self.nbins, -200.0), where=self._count > 0)

    def occupancy_pct(self):
        np = self.np
        return np.divide(self._above * 100.0, self.sweeps, out=np.zeros(self.nbins), where=self.sweeps > 0)

    def frequencies(self):
        np = self.np
        return self.start_hz + np.arange(self.nbins, dtype=float) * self.bin_width_hz

    def peaks(self, min_prominence_db: float = 6.0, max_peaks: int = 50) -> List[Dict[str, float]]:
        peaks = scan.find_peaks(
            list(self.max_hold),
            start_hz=self.start_hz,
            bin_width_hz=self.bin_width_hz,
            min_prominence_db=min_prominence_db,
            max_peaks=max_peaks,
        )
        occ = self.occupancy_pct()
        for peak in peaks:
            idx = int((peak["freq_hz"] - self.start_hz) / self.bin_width_hz)
            idx = max(0, min(self.nbins - 1, idx))
            peak["occupancy_pct"] = float(occ[idx])
        return peaks

    def write_csv(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            handle.write("freq_hz,max_hold_db,occupancy_pct\n")
            for freq, level, occ in zip(self.frequencies(), self.max_hold, self.occupancy_pct()):
                handle.write(f"{freq:.1f},{level:.2f},{occ:.2f}\n")

    def write_png(self, path: Path) -> bool:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except Exception:
            return False
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        import numpy as np
        fig, ax = plt.subplots(figsize=(14, 4))
        image = ax.imshow(
            self.max_hold[np.newaxis, :],
            aspect="auto",
            extent=[self.start_hz / 1e6, self.end_hz / 1e6, 0, 1],
            cmap="viridis",
            vmin=float(np.percentile(self.max_hold, 5)),
            vmax=float(np.max(self.max_hold)),
        )
        ax.set_xlabel("Frequency (MHz)")
        ax.set_yticks([])
        ax.set_title(f"Max-hold spectrum ({self.sweeps} sweeps)")
        fig.colorbar(image, ax=ax, label="dBFS")
        fig.tight_layout()
        fig.savefig(path, dpi=120)
        plt.close(fig)
        return True


def write_bookmarks(path: Path, peaks: Iterable[Dict[str, float]], *,
                    tag: str = "scan", modulation: str = "AM",
                    bandwidth_hz: int = 25000) -> int:
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    rows: List[str] = []
    existing = []
    if path.exists():
        existing = path.read_text(encoding="utf-8").splitlines()
    existing_freqs = set()
    for line in existing:
        if line.startswith("#") or not line.strip():
            continue
        first = line.split(";", 1)[0].strip()
        if first.lstrip("-").isdigit():
            existing_freqs.add(int(first))
    added = 0
    for peak in peaks:
        freq = int(round(peak["freq_hz"]))
        if freq in existing_freqs:
            continue
        name = f"scan {freq / 1e6:.3f} MHz"
        rows.append(f"{freq:>12}; {name:<25}; {modulation:<20}; {bandwidth_hz:>10}; {tag}")
        existing_freqs.add(freq)
        added += 1
    if not existing:
        text = [
            "# Tag name          ;  color",
            f"{tag:<19}; #729fcf",
            "",
            "# Frequency ; Name                     ; Modulation          ;  Bandwidth; Tags",
            *rows,
        ]
    else:
        text = existing
        # Add the tag if missing, before the first blank separator.
        if not any(line.lower().startswith(tag.lower() + " ") for line in text):
            insert_at = next((i for i, line in enumerate(text) if not line.strip()), 1)
            text.insert(insert_at, f"{tag:<19}; #729fcf")
        text.extend(rows)
    path.write_text("\n".join(text) + "\n", encoding="utf-8")
    return added


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7356)
    parser.add_argument("--start", type=int, required=True, help="start Hz")
    parser.add_argument("--end", type=int, required=True, help="end Hz")
    parser.add_argument("--pool", type=float, default=25000.0, help="FFT output bin width")
    parser.add_argument("--min-prominence", type=float, default=6.0)
    parser.add_argument("--max-peaks", type=int, default=50)
    parser.add_argument("--dwell", type=float, default=0.1, help="settle time per center")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between sweeps")
    parser.add_argument("--duration", type=float, default=60.0, help="total seconds")
    parser.add_argument("--sweeps", type=int, default=0, help="override duration")
    parser.add_argument("--sample-rate", type=float, help="FFT span if FFT Q omits it")
    parser.add_argument("--session-root", type=Path, default=Path("~/sdr/sessions"))
    parser.add_argument("--quota-gb", type=float, default=1.0)
    parser.add_argument("--retention-days", type=float, default=7.0)
    parser.add_argument("--bookmarks", type=Path, help="merge hits into this bookmarks.csv")
    parser.add_argument("--no-heatmap", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    sess = session_mod.Session.create(
        args.session_root,
        quota_bytes=int(args.quota_gb * 1024 ** 3),
        retention_days=args.retention_days,
        metadata={
            "kind": "scan_session",
            "start_hz": args.start,
            "end_hz": args.end,
            "pool_hz": args.pool,
        },
    )
    grid = GridAccumulator(args.start, args.end, args.pool)
    hits: Dict[int, Dict[str, float]] = {}
    deadline = time.monotonic() + args.duration
    sweep = 0
    try:
        with GqrxClient(args.host, args.port) as client:
            while True:
                if args.sweeps and sweep >= args.sweeps:
                    break
                if not args.sweeps and time.monotonic() >= deadline:
                    break
                peaks, spectra = scan.fft_sweep(
                    client,
                    start_hz=args.start,
                    end_hz=args.end,
                    pool_hz=args.pool,
                    min_prominence_db=args.min_prominence,
                    max_peaks=args.max_peaks,
                    dwell_s=args.dwell,
                    sample_rate_hz=args.sample_rate,
                    include_spectrum=True,
                )
                now = datetime.now(timezone.utc).isoformat()
                for spectrum in spectra:
                    grid.add(
                        spectrum["start_hz"],
                        spectrum["bin_width_hz"],
                        spectrum["values"],
                        margin_db=args.min_prominence,
                    )
                for peak in peaks:
                    key = int(round(peak["freq_hz"] / max(1.0, args.pool)))
                    current = hits.setdefault(
                        key,
                        {
                            "freq_hz": peak["freq_hz"],
                            "count": 0,
                            "max_level_db": -200.0,
                            "max_prominence_db": 0.0,
                        },
                    )
                    current["count"] += 1
                    current["max_level_db"] = max(current["max_level_db"], peak["level_db"])
                    current["max_prominence_db"] = max(current["max_prominence_db"], peak["prominence_db"])
                sess.record_scan({"kind": "sweep", "time": now, "sweep": sweep, "peaks": peaks})
                sweep += 1
                if args.sweeps and sweep >= args.sweeps:
                    break
                time.sleep(max(0.0, args.interval))
    except (GqrxError, OSError, scan.FFTUnavailable, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    summary_peaks = sorted(hits.values(), key=lambda item: item["max_prominence_db"], reverse=True)
    summary = {
        "start_hz": args.start,
        "end_hz": args.end,
        "pool_hz": args.pool,
        "sweeps": sweep,
        "peaks": summary_peaks,
    }
    sess.write_json(sess.path / "scan_summary.json", summary)
    if not args.no_heatmap:
        grid.write_csv(sess.path / "heatmap.csv")
        grid.write_png(sess.path / "heatmap.png")
    if args.bookmarks:
        added = write_bookmarks(
            args.bookmarks,
            summary_peaks,
            bandwidth_hz=int(args.pool),
        )
        print(f"bookmarks: added {added} to {args.bookmarks}", file=sys.stderr)
    print(f"session: {sess.path} sweeps={sweep} peaks={len(summary_peaks)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
