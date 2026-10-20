import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import array
import os
import socket
import struct
import unittest

import live_decode
import shm_iq


class ShmIqTest(unittest.TestCase):
    def make_ring(self, payload=b"\x7f\x80" * 64, rate=250000):
        memfd = os.memfd_create("gqrx-test-ring")
        event_fd = os.eventfd(0, os.EFD_NONBLOCK | os.EFD_SEMAPHORE)
        shm_iq.write_test_ring(memfd, event_fd, payload, rate)
        return memfd, event_fd, payload

    def test_attach_and_read(self):
        memfd, event_fd, payload = self.make_ring()
        reader = shm_iq.ShmIqReader(memfd, event_fd)
        try:
            self.assertEqual(reader.sample_rate, 250000)
            self.assertEqual(reader.sample_size, 2)
            self.assertEqual(reader.read_chunk(4096, 0), payload)
            self.assertEqual(reader.read_chunk(4096, 0), b"")
        finally:
            reader.close()

    def test_scm_rights_handoff(self):
        memfd, event_fd, payload = self.make_ring()
        left, right = socket.socketpair()
        try:
            fds = array.array("i", [memfd, event_fd])
            left.sendmsg([b"x"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, fds)])
            got_memfd, got_event = shm_iq.recv_fds_from_socket(right)
            reader = shm_iq.ShmIqReader(got_memfd, got_event)
            try:
                self.assertEqual(reader.read_chunk(4096, 0), payload)
            finally:
                reader.close()
        finally:
            left.close()
            right.close()
            os.close(memfd)
            os.close(event_fd)

    def test_overflow_drops_to_ring_size(self):
        memfd = os.memfd_create("gqrx-test-overflow")
        event_fd = os.eventfd(0, os.EFD_NONBLOCK | os.EFD_SEMAPHORE)
        bufsize = 4096
        os.ftruncate(memfd, shm_iq.HEADER_SIZE + bufsize)
        mm = __import__("mmap").mmap(memfd, 0, access=__import__("mmap").ACCESS_WRITE)
        shm_iq.HEADER.pack_into(mm, 0, shm_iq.MAGIC, shm_iq.VERSION,
                                bufsize * 3 + 1, 0, bufsize, 250000, 2)
        mm.close()
        reader = shm_iq.ShmIqReader(memfd, event_fd)
        try:
            available = reader.available()
            self.assertLessEqual(available, bufsize)
            self.assertEqual(available % 2, 0)
        finally:
            reader.close()

    def test_build_command(self):
        command = live_decode.build_rtl433_command(
            "rtl433", rate_hz=250000, frequency_hz=433900000, sample_size=2,
            flex="n=x,m=OOK_PWM,s=1,l=2", output=Path("events.jsonl"),
        )
        self.assertEqual(command[2], "cu8:-")
        self.assertIn("250000", command)
        self.assertIn("433900000", command)
        self.assertIn("n=x,m=OOK_PWM,s=1,l=2", command)
        self.assertIn("json:events.jsonl", command)


if __name__ == "__main__":
    unittest.main()
