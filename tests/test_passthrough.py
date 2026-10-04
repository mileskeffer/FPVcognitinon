import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import backup_esp8285


class PassthroughTests(unittest.TestCase):
    def run_setup(self, response, baud=0):
        with mock.patch.object(backup_esp8285.serial, "Serial") as serial_mock, \
             mock.patch.object(backup_esp8285, "read_available", side_effect=[b"#", response]), \
             mock.patch.object(backup_esp8285, "cli_command", side_effect=[
                 b"CRSF", b"OFF", b"OFF", b"serial UART3 64 115200 57600 0 115200"
             ]):
            backup_esp8285.enable_passthrough("COM4", "UART3", baud)
            serial_mock.return_value.__enter__.return_value.write.assert_any_call(
                f"serialpassthrough UART3 {baud} rxtx\r".encode("ascii")
            )

    def test_confirms_dynamic_baud_and_avoids_trailing_newline(self):
        self.run_setup(b"Port1 baud rate change over USB enabled.\r\nForwarding")

    def test_rejects_cli_error_instead_of_attempting_flash(self):
        with self.assertRaisesRegex(RuntimeError, "did not confirm"):
            self.run_setup(b"Invalid port1")

    def test_rejects_missing_dynamic_baud_confirmation(self):
        with self.assertRaisesRegex(RuntimeError, "USB-controlled"):
            self.run_setup(b"Forwarding")

    def test_fixed_baud_does_not_require_dynamic_confirmation(self):
        self.run_setup(b"Forwarding", 115200)
