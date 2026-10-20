#!/usr/bin/env python3
"""Minimal consumer for gqrx's memfd/eventfd CU8 IQ ring.

The producer is `shm_iq_sink` (WITH_SHM_IQ_SINK). It hands two descriptors to
an unrelated process over a Unix socket using SCM_RIGHTS. This module attaches
to the mapping, verifies the header, and yields CU8/CS16 bytes without a
per-sample Python loop.
"""

from __future__ import annotations

import mmap
import os
import select
import socket
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

MAGIC = 0x52494E47
VERSION = 1
HEADER = struct.Struct("<IIQQQII")
HEADER_SIZE = HEADER.size
DEFAULT_SOCKET = "/tmp/gqrx_iq.sock"


class ShmIqError(RuntimeError):
    pass


@dataclass
class ShmHeader:
    magic: int
    version: int
    head: int
    tail: int
    bufsize: int
    sample_rate: int
    sample_size: int


def recv_fds_from_socket(sock: socket.socket) -> Tuple[int, int]:
    msg, ancdata, _flags, _addr = sock.recvmsg(1, socket.CMSG_SPACE(2 * 4))
    if len(msg) != 1:
        raise ShmIqError("producer did not send the expected fd message")
    for level, kind, data in ancdata:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            if len(data) < 2 * 4:
                raise ShmIqError("SCM_RIGHTS message did not contain two fds")
            fds = struct.unpack(f"{len(data) // 4}i", data)
            return fds[0], fds[1]
    raise ShmIqError("SCM_RIGHTS message missing")


def recv_fds(path: str = DEFAULT_SOCKET, timeout: float = 35.0) -> Tuple[int, int]:
    deadline = time.monotonic() + timeout
    last_error: Optional[Exception] = None
    while time.monotonic() < deadline:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(max(0.2, min(2.0, deadline - time.monotonic())))
                sock.connect(path)
                return recv_fds_from_socket(sock)
        except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError) as exc:
            last_error = exc
            time.sleep(0.1)
    raise ShmIqError(f"could not attach to {path}: {last_error}")


class ShmIqReader:
    def __init__(self, memfd: int, event_fd: int):
        self.memfd = memfd
        self.event_fd = event_fd
        self.mm = mmap.mmap(memfd, 0, access=mmap.ACCESS_WRITE)
        if len(self.mm) < HEADER_SIZE:
            raise ShmIqError("shared mapping is smaller than the header")
        fields = HEADER.unpack_from(self.mm, 0)
        self.header = ShmHeader(*fields)
        if self.header.magic != MAGIC or self.header.version != VERSION:
            raise ShmIqError(
                f"bad ring header magic/version: 0x{self.header.magic:x}/{self.header.version}"
            )
        if self.header.sample_size not in (2, 4):
            raise ShmIqError(f"unsupported sample size: {self.header.sample_size}")
        if self.header.bufsize <= 0:
            raise ShmIqError("invalid ring size")
        self._data_offset = HEADER_SIZE

    @property
    def sample_rate(self) -> int:
        return self.header.sample_rate

    @property
    def sample_size(self) -> int:
        return self.header.sample_size

    def _indices(self) -> Tuple[int, int]:
        head, tail = struct.unpack_from("<QQ", self.mm, 8)
        return head, tail

    def _set_tail(self, tail: int) -> None:
        struct.pack_into("<Q", self.mm, 16, tail)

    def available(self) -> int:
        head, tail = self._indices()
        if head - tail > self.header.bufsize:
            # The producer outran us; drop the oldest whole samples.
            tail = head - self.header.bufsize
            tail += tail % self.header.sample_size
            self._set_tail(tail)
        available = head - tail
        return available - (available % self.header.sample_size)

    def read_chunk(self, max_bytes: int = 65536, timeout_ms: int = 500) -> bytes:
        avail = self.available()
        if avail == 0:
            if timeout_ms != 0 and self.event_fd >= 0:
                poller = select.poll()
                poller.register(self.event_fd, select.POLLIN)
                if not poller.poll(timeout_ms):
                    return b""
                try:
                    os.read(self.event_fd, 8)
                except BlockingIOError:
                    pass
            avail = self.available()
        if avail <= 0:
            return b""

        head, tail = self._indices()
        count = min(avail, max_bytes)
        count -= count % self.header.sample_size
        if count <= 0:
            return b""
        start = tail % self.header.bufsize
        end = start + count
        if end <= self.header.bufsize:
            data = self.mm[self._data_offset + start:self._data_offset + end]
        else:
            first = self.header.bufsize - start
            data = (
                self.mm[self._data_offset + start:self._data_offset + self.header.bufsize]
                + self.mm[self._data_offset:self._data_offset + (count - first)]
            )
        self._set_tail(tail + count)
        return bytes(data)

    def close(self) -> None:
        try:
            self.mm.close()
        finally:
            for fd in (self.memfd, self.event_fd):
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass


def attach(path: str = DEFAULT_SOCKET, timeout: float = 35.0) -> ShmIqReader:
    return ShmIqReader(*recv_fds(path, timeout))


def write_test_ring(memfd: int, event_fd: int, payload: bytes, sample_rate: int = 250000) -> None:
    """Test helper: fill a ring with payload and signal eventfd."""
    bufsize = max(len(payload), 4096)
    os.ftruncate(memfd, HEADER_SIZE + bufsize)
    mm = mmap.mmap(memfd, 0, access=mmap.ACCESS_WRITE)
    HEADER.pack_into(mm, 0, MAGIC, VERSION, len(payload), 0, bufsize, sample_rate, 2)
    mm[HEADER_SIZE:HEADER_SIZE + len(payload)] = payload
    os.write(event_fd, struct.pack("<Q", 1))
    mm.close()
