import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from msp import (AUX1, AUX2, THROTTLE, FrameParser, MspLink,
                 describe_disable_flags, encode_request, parse_status, rc_payload)

# MSP_STATUS captured from the Air75 II (Betaflight 2026.6, USB only, disarmed).
AIR75_STATUS = bytes.fromhex("340100002100040000000025000000001d04001000003100040300")


def response(command, payload=b"", marker=b">"):
    checksum = len(payload) ^ command
    for value in payload:
        checksum ^= value
    return b"$M" + marker + bytes((len(payload), command)) + payload + bytes((checksum,))


class MspTests(unittest.TestCase):
    def test_encode_matches_reference_frame(self):
        self.assertEqual(encode_request(101), b"$M<\x00\x65\x65")

    def test_rc_payload_clamps_and_packs_little_endian(self):
        self.assertEqual(rc_payload([900, 1500, 2100]), struct.pack("<3H", 1000, 1500, 2000))

    def test_parser_handles_split_noise_errors_and_bad_checksums(self):
        parser = FrameParser()
        good = response(101, b"\x01\x02")
        bad = bytearray(response(105, b"\x03")); bad[-1] ^= 0xFF
        self.assertEqual(parser.feed(b"junk" + good[:4]), [])
        frames = parser.feed(good[4:] + bytes(bad) + response(200, marker=b"!"))
        self.assertEqual(frames, [(101, b"\x01\x02", True), (200, b"", False)])
        self.assertEqual(parser.errors, 1)

    def test_parse_real_air75_status(self):
        armed, flags = parse_status(AIR75_STATUS)
        self.assertFalse(armed)
        self.assertEqual(describe_disable_flags(flags), "RXLOSS RPMFILTER")

    def test_armed_bit(self):
        status = bytearray(AIR75_STATUS); status[6] |= 1
        self.assertTrue(parse_status(bytes(status))[0])

    def test_disarmed_channels_force_arm_low_and_min_throttle(self):
        link = MspLink.__new__(MspLink)
        link.roll = link.pitch = link.yaw = 1500
        link.throttle, link.armed = 1600, False
        channels = link.channels_locked()
        self.assertEqual((channels[THROTTLE], channels[AUX1], channels[AUX2]), (1000, 1000, 1000))
        link.armed = True
        channels = link.channels_locked()
        self.assertEqual((channels[THROTTLE], channels[AUX1]), (1600, 2000))


if __name__ == "__main__":
    unittest.main()
