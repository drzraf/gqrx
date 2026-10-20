#!/usr/bin/env python3
"""Attach to gqrx's SHM IQ stream and pipe CU8/CS16 directly to rtl_433.

The stream is opt-in: pass --arm to send ``U IQSTREAM 1`` through the gqrx
remote-control connection, or start it yourself from another client.
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Sequence

import shm_iq
from gqrxctl import GqrxClient, GqrxError


def find_rtl433(explicit: Optional[str] = None) -> str:
    candidate = explicit or os.environ.get("RTL433")
    if candidate:
        return candidate
    found = shutil.which("rtl_433")
    if found:
        return found
    raise RuntimeError("rtl_433 not found; pass --rtl433 or set RTL433")


def build_rtl433_command(
    rtl433: str,
    *,
    rate_hz: int,
    frequency_hz: int,
    sample_size: int,
    flex: Optional[str] = None,
    output: Optional[Path] = None,
    extra: Optional[Sequence[str]] = None,
) -> list[str]:
    fmt = "cu8" if sample_size == 2 else "cs16"
    command = [
        rtl433,
        "-r",
        f"{fmt}:-",
        "-s",
        str(int(rate_hz)),
        "-f",
        str(int(frequency_hz)),
        "-M",
        "level",
        "-M",
        "noise",
        "-M",
        "time:unix:usec:utc",
    ]
    if flex:
        command.extend(["-X", flex])
    if output:
        command.extend(["-F", f"json:{output}"])
    else:
        command.extend(["-F", "json"])
    if extra:
        command.extend(extra)
    return command


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default=shm_iq.DEFAULT_SOCKET)
    parser.add_argument("--attach-timeout", type=float, default=35.0)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7356)
    parser.add_argument("--arm", action="store_true", help="send U IQSTREAM 1 first")
    parser.add_argument("--freq", type=int, required=True, help="hardware center (Hz)")
    parser.add_argument("--rtl433")
    parser.add_argument("--flex", help="rtl_433 -X spec")
    parser.add_argument("--output", type=Path, help="JSONL output")
    parser.add_argument("--raw-out", type=Path, help="optional CU8 copy (use sparingly)")
    parser.add_argument("--duration", type=float, default=0.0)
    parser.add_argument("--extra", nargs=argparse.REMAINDER, help="extra rtl_433 args after --")
    args = parser.parse_args(argv)

    gqrx: Optional[GqrxClient] = None
    reader: Optional[shm_iq.ShmIqReader] = None
    proc: Optional[subprocess.Popen] = None
    raw = None
    armed = False
    try:
        rtl433 = find_rtl433(args.rtl433)
        if args.arm:
            gqrx = GqrxClient(args.host, args.port).connect()
            reply = gqrx.command("U IQSTREAM 1")[0]
            if reply != "RPRT 0":
                raise RuntimeError(f"gqrx rejected IQSTREAM: {reply}")
            armed = True

        reader = shm_iq.attach(args.socket, args.attach_timeout)
        command = build_rtl433_command(
            rtl433,
            rate_hz=reader.sample_rate,
            frequency_hz=args.freq,
            sample_size=reader.sample_size,
            flex=args.flex,
            output=args.output,
            extra=args.extra,
        )
        print("rtl_433:", " ".join(command), file=sys.stderr)
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, bufsize=0)
        if args.raw_out:
            raw = args.raw_out.open("wb")
        deadline = None if args.duration <= 0 else time.monotonic() + args.duration
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                break
            if proc.poll() is not None:
                break
            chunk = reader.read_chunk(65536, 250)
            if not chunk:
                continue
            if raw is not None:
                raw.write(chunk)
            try:
                proc.stdin.write(chunk)
            except BrokenPipeError:
                break
    except (GqrxError, OSError, RuntimeError, shm_iq.ShmIqError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if proc is not None:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except OSError:
                pass
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        if raw is not None:
            raw.close()
        if reader is not None:
            reader.close()
        if gqrx is not None:
            if armed:
                try:
                    gqrx.command("U IQSTREAM 0")
                except GqrxError:
                    pass
            gqrx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
