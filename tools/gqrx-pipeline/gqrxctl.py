#!/usr/bin/env python3
"""Small dependency-free client for the gqrx TCP remote protocol.

The protocol is line-oriented: one request line, one or more response lines.
See ``resources/remote-control.txt`` in the gqrx tree for the command list.
"""

from __future__ import annotations

import argparse
import socket
import sys
from contextlib import AbstractContextManager
from typing import Iterable, List, Optional, Sequence


class GqrxError(RuntimeError):
    """Raised when gqrx rejects a command or the connection fails."""


class GqrxClient(AbstractContextManager):
    def __init__(self, host: str = "127.0.0.1", port: int = 7356, timeout: float = 2.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._reader = None

    def connect(self) -> "GqrxClient":
        if self._sock is None:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self._sock.settimeout(self.timeout)
            self._reader = self._sock.makefile("rb")
        return self

    def close(self) -> None:
        if self._reader is not None:
            try:
                self._reader.close()
            finally:
                self._reader = None
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def __enter__(self) -> "GqrxClient":
        return self.connect()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def command(self, line: str, reply_lines: int = 1) -> List[str]:
        if reply_lines < 1:
            raise ValueError("reply_lines must be >= 1")
        self.connect()
        assert self._sock is not None
        assert self._reader is not None
        if "\n" in line or "\r" in line:
            raise ValueError("command must be a single line")
        try:
            self._sock.sendall(line.encode("ascii") + b"\n")
            replies: List[str] = []
            for _ in range(reply_lines):
                raw = self._reader.readline()
                if not raw:
                    raise GqrxError("gqrx closed the connection")
                replies.append(raw.decode("ascii", "replace").rstrip("\r\n"))
            return replies
        except OSError as exc:
            self.close()
            raise GqrxError(str(exc)) from exc

    def query(self, line: str) -> str:
        reply = self.command(line, 1)[0]
        if reply.startswith("RPRT "):
            raise GqrxError(reply)
        return reply

    def set_command(self, line: str) -> None:
        reply = self.command(line, 1)[0]
        if reply != "RPRT 0":
            raise GqrxError(reply)

    def get_frequency(self) -> int:
        return int(float(self.query("f")))

    def set_frequency(self, hz: int) -> None:
        self.set_command(f"F {int(hz)}")

    def get_mode(self) -> tuple[str, int]:
        mode, passband = self.command("m", 2)
        return mode, int(float(passband))

    def set_mode(self, mode: str, passband_hz: Optional[int] = None) -> None:
        line = f"M {mode}"
        if passband_hz is not None:
            line += f" {int(passband_hz)}"
        self.set_command(line)

    def get_strength(self) -> float:
        return float(self.query("l STRENGTH"))

    def get_squelch(self) -> float:
        return float(self.query("l SQL"))

    def set_squelch(self, dbfs: float) -> None:
        self.set_command(f"L SQL {float(dbfs):.1f}")

    def get_audio_gain(self) -> float:
        return float(self.query("l AF"))

    def set_audio_gain(self, dbfs: float) -> None:
        self.set_command(f"L AF {float(dbfs):.1f}")

    def list_gains(self) -> List[str]:
        names = self.query("l ?").split()
        return [name[:-5] for name in names if name.endswith("_GAIN")]

    def get_gain(self, name: str) -> float:
        return float(self.query(f"l {name}_GAIN"))

    def set_gain(self, name: str, value: float) -> None:
        self.set_command(f"L {name}_GAIN {float(value):.1f}")

    def get_function(self, name: str) -> str:
        return self.query(f"u {name}")

    def set_function(self, name: str, enabled: bool) -> None:
        self.set_command(f"U {name} {1 if enabled else 0}")

    def dsp(self, enabled: bool) -> None:
        self.set_function("DSP", enabled)

    def iq_record(self, enabled: bool) -> None:
        self.set_function("IQRECORD", enabled)

    def audio_record(self, enabled: bool) -> None:
        self.set_function("RECORD", enabled)

    def mute(self, enabled: bool) -> None:
        self.set_function("MUTE", enabled)

    def version(self) -> str:
        return self.query("_")


def _print_replies(replies: Iterable[str]) -> None:
    for reply in replies:
        print(reply)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7356)
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--lines", type=int, default=1, help="number of response lines to read")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="remote command, e.g. 'l STRENGTH'")
    args = parser.parse_args(argv)

    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("missing remote command")

    try:
        with GqrxClient(args.host, args.port, args.timeout) as client:
            _print_replies(client.command(" ".join(command), args.lines))
    except (GqrxError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
