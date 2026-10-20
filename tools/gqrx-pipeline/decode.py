#!/usr/bin/env python3
"""Generic rtl_433 wrapper for gqrx recordings.

The wrapper is deliberately thin: it builds the correct ``-r <format>:<file>``
specifier, passes the true sample rate/center, and appends JSONL. It does not
assume a modulation or protocol.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Sequence


class DecodeError(RuntimeError):
    pass


def detect_format(path: Path, explicit: Optional[str] = None) -> str:
    if explicit:
        return explicit
    suffixes = [suffix.lower() for suffix in path.suffixes]
    name = path.name.lower()
    if name.endswith(".sigmf-data"):
        return "cf32"
    if name.endswith(".complex"):
        return "cf32"
    if name.endswith(".cu8") or name.endswith(".iq"):
        return "cu8"
    if name.endswith(".cs16"):
        return "cs16"
    if name.endswith(".cs8"):
        return "cs8"
    if name.endswith(".raw"):
        return "cf32"  # gqrx writes CF32; pass --format for other raw data
    if name.endswith(".wav"):
        raise DecodeError("WAV is audio; decode from IQ or use an audio-specific tool")
    if ".sigmf-data" in suffixes:
        return "cf32"
    raise DecodeError(f"cannot infer format for {path}; pass --format")


def find_rtl433(explicit: Optional[str] = None, env: Optional[str] = None) -> str:
    candidate = explicit or env or os.environ.get("RTL433")
    if candidate:
        return candidate
    found = shutil.which("rtl_433")
    if found:
        return found
    raise DecodeError("rtl_433 not found; pass --rtl433 or set RTL433")


def build_command(
    rtl433: str,
    path: Path,
    *,
    rate_hz: int,
    frequency_hz: int,
    fmt: str,
    flex: Optional[str] = None,
    output: Optional[Path] = None,
    extra: Optional[Sequence[str]] = None,
    meta: bool = True,
) -> List[str]:
    command = [
        rtl433,
        "-r",
        f"{fmt}:{path}",
        "-s",
        str(int(rate_hz)),
        "-f",
        str(int(frequency_hz)),
    ]
    if flex:
        command.extend(["-X", flex])
    if meta:
        command.extend(["-M", "level", "-M", "noise", "-M", "time:unix:usec:utc"])
    if output:
        command.extend(["-F", f"json:{output}"])
    else:
        command.extend(["-F", "json"])
    if extra:
        command.extend(extra)
    return command


def decode_file(
    rtl433: str,
    path: Path,
    *,
    rate_hz: int,
    frequency_hz: int,
    fmt: Optional[str] = None,
    flex: Optional[str] = None,
    output: Optional[Path] = None,
    extra: Optional[Sequence[str]] = None,
    dry_run: bool = False,
) -> List[str]:
    path = Path(path)
    if not path.exists():
        raise DecodeError(f"file not found: {path}")
    fmt = detect_format(path, fmt)
    if output is not None:
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
    command = build_command(
        rtl433,
        path,
        rate_hz=rate_hz,
        frequency_hz=frequency_hz,
        fmt=fmt,
        flex=flex,
        output=output,
        extra=extra,
    )
    if dry_run:
        print(" ".join(command))
        return command
    subprocess.run(command, check=True)
    return command


def iter_candidates(directory: Path) -> Iterable[Path]:
    suffixes = {".raw", ".iq", ".cu8", ".cs16", ".cs8", ".complex", ".sigmf-data"}
    for path in sorted(Path(directory).rglob("*")):
        if path.is_file() and path.suffix.lower() in suffixes:
            yield path


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", type=Path)
    source.add_argument("--dir", type=Path)
    parser.add_argument("--rate", type=int, help="true IQ sample rate (Hz)")
    parser.add_argument("--freq", type=int, help="center frequency (Hz)")
    parser.add_argument("--format", choices=["cf32", "cu8", "cs8", "cs16"])
    parser.add_argument("--flex", help="rtl_433 -X spec")
    parser.add_argument("--output", type=Path, help="JSONL output (appended)")
    parser.add_argument("--rtl433", help="path to rtl_433")
    parser.add_argument("--extra", nargs=argparse.REMAINDER, help="extra rtl_433 args after --")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.rate is None or args.freq is None:
        parser.error("--rate and --freq are required")
    try:
        rtl433 = find_rtl433(args.rtl433)
        files = [args.file] if args.file else list(iter_candidates(args.dir))
        if not files:
            print("no candidate files", file=sys.stderr)
            return 1
        for path in files:
            decode_file(
                rtl433,
                path,
                rate_hz=args.rate,
                frequency_hz=args.freq,
                fmt=args.format,
                flex=args.flex,
                output=args.output,
                extra=args.extra,
                dry_run=args.dry_run,
            )
    except (DecodeError, OSError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
