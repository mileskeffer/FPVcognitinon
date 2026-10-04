import json
import unittest
from unittest import mock

from receiver_diagnostics import fetch_diagnostics


class ReceiverDiagnosticsTests(unittest.TestCase):
    def test_uses_separate_read_only_port(self):
        response = {"firmware": "air75-diag-2", "boot_id": 123, "fault": {"cause": 1}}
        with mock.patch("receiver_diagnostics.socket.socket") as factory:
            sock = factory.return_value.__enter__.return_value
            sock.recv.return_value = json.dumps(response).encode()
            self.assertEqual(fetch_diagnostics(), response)
            sock.connect.assert_called_once_with(("192.168.4.1", 4211))
            sock.send.assert_called_once_with(b"A75D")

    def test_rejects_other_protocol(self):
        with mock.patch("receiver_diagnostics.socket.socket") as factory:
            factory.return_value.__enter__.return_value.recv.return_value = b'{}'
            with self.assertRaises(ValueError):
                fetch_diagnostics()

    def test_deadline_does_not_send_control_packets(self):
        with mock.patch("receiver_diagnostics.socket.socket") as factory:
            with self.assertRaises(TimeoutError):
                fetch_diagnostics(timeout=0)
            factory.return_value.__enter__.return_value.send.assert_not_called()
