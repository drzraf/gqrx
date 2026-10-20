import socket
import threading
import unittest

from gqrxctl import GqrxClient, GqrxError


class FakeGqrx(threading.Thread):
    def __init__(self, responses):
        super().__init__(daemon=True)
        self.responses = responses
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.received = []

    def run(self):
        conn, _ = self.server.accept()
        with conn:
            reader = conn.makefile("rb")
            writer = conn.makefile("wb")
            for raw in reader:
                line = raw.decode().rstrip("\r\n")
                self.received.append(line)
                reply = self.responses.get(line, "RPRT 1")
                writer.write((reply + "\n").encode())
                writer.flush()

    def stop(self):
        self.server.close()


class GqrxClientTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGqrx(
            {
                "f": "433900000",
                "m": "AM\n20000",
                "l STRENGTH": "-42.5",
                "l SQL": "-55.0",
                "l ?": "SQL STRENGTH AF LNA_GAIN IF_GAIN",
                "L SQL -50.0": "RPRT 0",
                "L LNA_GAIN 24.0": "RPRT 0",
                "F 145500000": "RPRT 0",
                "M FM 15000": "RPRT 0",
                "U IQRECORD 1": "RPRT 0",
                "u IQRECORD": "1",
                "_": "Gqrx 2.17.7",
            }
        )
        self.fake.start()

    def tearDown(self):
        self.fake.stop()

    def client(self):
        return GqrxClient(port=self.fake.port, timeout=1.0)

    def test_reads_single_and_multi_line(self):
        with self.client() as client:
            self.assertEqual(client.get_frequency(), 433900000)
            self.assertEqual(client.get_mode(), ("AM", 20000))
            self.assertEqual(client.get_strength(), -42.5)
            self.assertEqual(client.list_gains(), ["LNA", "IF"])
            self.assertEqual(client.version(), "Gqrx 2.17.7")

    def test_writes_commands(self):
        with self.client() as client:
            client.set_frequency(145500000)
            client.set_mode("FM", 15000)
            client.set_squelch(-50)
            client.set_gain("LNA", 24)
            client.iq_record(True)
        self.assertIn("F 145500000", self.fake.received)
        self.assertIn("M FM 15000", self.fake.received)
        self.assertIn("L SQL -50.0", self.fake.received)
        self.assertIn("L LNA_GAIN 24.0", self.fake.received)
        self.assertIn("U IQRECORD 1", self.fake.received)

    def test_rejected_command_raises(self):
        with self.client() as client:
            with self.assertRaises(GqrxError):
                client.set_command("U NOPE 1")


if __name__ == "__main__":
    unittest.main()
