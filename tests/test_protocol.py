import errno
import socket
import struct
import unittest
import zlib
from unittest import mock

from dronelink import (
    ACK_PACKET,
    CONTROL_PACKET,
    FLAG_ARM,
    FLAG_KILL,
    MAX_ACKS_PER_TICK,
    DroneLink,
    build_control_packet,
    parse_ack,
)


class ProtocolTests(unittest.TestCase):
    def test_ack_flood_does_not_starve_sends_or_watchdog(self):
        fake_socket = mock.Mock()
        # Keep traffic available beyond the per-tick limit. A formerly
        # unbounded receive loop would consume the entire queue.
        fake_socket.recvfrom.side_effect = (
            [(b"invalid", ("192.168.4.1", 4210))] * (MAX_ACKS_PER_TICK * 2)
            + [BlockingIOError()]
        )
        with mock.patch.object(socket, "socket", return_value=fake_socket):
            link = DroneLink()
        link.resume()
        link._receive_acks()
        self.assertEqual(fake_socket.recvfrom.call_count, MAX_ACKS_PER_TICK)
        link._send_current()
        fake_socket.sendto.assert_called_once()
        link._last_control_update -= 1.0
        link._receive_acks()
        link._send_current()
        self.assertTrue(link.paused)
        self.assertIn("CONTROL LOOP STALLED", link.watchdog_error)

    def test_wifi_requests_are_released_if_sender_cannot_start(self):
        with mock.patch.object(socket, "socket"), \
             mock.patch("dronelink.WifiControlSession") as wifi, \
             mock.patch("dronelink.threading.Thread") as thread:
            thread.return_value.start.side_effect = RuntimeError("no thread available")
            link = DroneLink()
            with self.assertRaises(RuntimeError):
                link.start()
            self.assertFalse(link._running)
            wifi.return_value.close.assert_called_once()
            link.stop()

    def test_packet_trace_records_send_failures_and_kill_flags(self):
        fake_socket = mock.Mock()
        with mock.patch.object(socket, "socket", return_value=fake_socket):
            link = DroneLink()
        link.resume()
        link._send_current()
        fake_socket.sendto.side_effect = OSError("network unavailable")
        link._send_current()
        fake_socket.sendto.side_effect = None
        before_kill = link.diagnostics()
        link.kill()
        self.assertEqual([event["kind"] for event in before_kill["events"]],
                         ["send", "send_error"])
        self.assertEqual(before_kill["events"][0]["flags"], 0)
        self.assertIn("network unavailable", before_kill["events"][1]["error"])
        self.assertEqual(len(before_kill["events"]), 2)
        self.assertTrue(all(event["flags"] == FLAG_KILL
                            for event in link.diagnostics()["events"][-5:]))

    def test_stale_input_disarms_and_records_watchdog_reason(self):
        fake_socket = mock.Mock()
        with mock.patch.object(socket, "socket", return_value=fake_socket):
            link = DroneLink()
        link.resume()
        link.armed = True
        link.throttle = 1700
        link._last_control_update -= 1.0
        link._send_current()
        self.assertTrue(link.paused)
        self.assertFalse(link.armed)
        self.assertIn("CONTROL LOOP STALLED", link.watchdog_error)
        self.assertEqual(fake_socket.sendto.call_count, 5)
        for call in fake_socket.sendto.call_args_list:
            fields = CONTROL_PACKET.unpack(call.args[0])
            self.assertEqual(fields[2], FLAG_KILL)
            self.assertEqual(fields[8], 1000)

    def test_unavailable_bind_address_falls_back_to_wildcard(self):
        fake_socket = mock.Mock()
        fake_socket.bind.side_effect = [
            OSError(errno.EADDRNOTAVAIL, "address unavailable"),
            None,
        ]
        with mock.patch.object(socket, "socket", return_value=fake_socket):
            link = DroneLink(bind_ip="192.168.4.2")

        self.assertIsNotNone(link.bind_warning)
        self.assertEqual(
            fake_socket.bind.call_args_list,
            [mock.call(("192.168.4.2", 0)), mock.call(("", 0))],
        )

    def test_stop_sends_disarm_burst_before_closing(self):
        fake_socket = mock.Mock()
        with mock.patch.object(socket, "socket", return_value=fake_socket):
            link = DroneLink()
        link._running = True
        link.armed = True
        link.throttle = 1700

        link.stop()

        self.assertEqual(fake_socket.sendto.call_count, 5)
        for call in fake_socket.sendto.call_args_list:
            packet, destination = call.args
            unpacked = CONTROL_PACKET.unpack(packet)
            flags = unpacked[2]
            throttle = unpacked[8]
            self.assertEqual(flags & FLAG_ARM, 0)
            self.assertEqual(flags & FLAG_KILL, 0)
            self.assertEqual(throttle, 1000)
            self.assertEqual(destination, ("192.168.4.1", 4210))
        fake_socket.close.assert_called_once()

    def test_control_packet_layout_and_crc(self):
        packet = build_control_packet(
            session=0x11223344,
            sequence=7,
            roll=1500,
            pitch=1400,
            throttle=1000,
            yaw=1600,
            armed=True,
        )
        self.assertEqual(len(packet), 28)
        self.assertEqual(packet[:4], b"A75W")
        self.assertEqual(packet[4], 1)
        self.assertEqual(packet[5] & 1, 1)
        self.assertEqual(struct.unpack_from("<H", packet, 6)[0], 28)
        self.assertEqual(struct.unpack_from("<I", packet, 8)[0], 0x11223344)
        self.assertEqual(struct.unpack_from("<I", packet, 24)[0], zlib.crc32(packet[:24]) & 0xFFFFFFFF)

    def test_ack_validation(self):
        body = struct.pack("<4sBBHII", b"A75A", 1, 2, ACK_PACKET.size, 10, 20)
        packet = body + struct.pack("<I", zlib.crc32(body) & 0xFFFFFFFF)
        self.assertEqual(parse_ack(packet), (2, 10, 20))

        corrupt = packet[:-1] + bytes([packet[-1] ^ 1])
        self.assertIsNone(parse_ack(corrupt))


if __name__ == "__main__":
    unittest.main()
