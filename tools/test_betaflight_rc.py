"""Verify neutral disarmed Wi-Fi controls reach Betaflight through CRSF."""

from __future__ import annotations

import argparse
import struct
import sys
import time
from pathlib import Path

import serial

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dronelink import DroneLink


MSP_RC = 105


def read_msp_rc(com_port: str) -> list[int]:
    request = b"$M<" + bytes((0, MSP_RC, MSP_RC))
    with serial.Serial(com_port, 115200, timeout=0.05, write_timeout=1) as port:
        port.reset_input_buffer()
        port.write(request)
        port.flush()
        deadline = time.monotonic() + 2.0
        data = bytearray()
        while time.monotonic() < deadline:
            data.extend(port.read(port.in_waiting or 1))
            start = data.find(b"$M>")
            if start < 0 or len(data) < start + 6:
                continue
            length = data[start + 3]
            frame_end = start + 6 + length
            if len(data) < frame_end:
                continue
            command = data[start + 4]
            payload = bytes(data[start + 5 : start + 5 + length])
            received_checksum = data[start + 5 + length]
            checksum = length ^ command
            for value in payload:
                checksum ^= value
            if command != MSP_RC or checksum != received_checksum:
                raise RuntimeError("Invalid MSP_RC response")
            if len(payload) % 2:
                raise RuntimeError("MSP_RC payload has an odd byte count")
            return list(struct.unpack(f"<{len(payload) // 2}H", payload))
    raise TimeoutError("Betaflight did not return MSP_RC within two seconds")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="COM4")
    parser.add_argument("--bind-ip", default="192.168.4.2")
    parser.add_argument("--drone-ip", default="192.168.4.1")
    args = parser.parse_args()

    link = DroneLink(
        ip=args.drone_ip,
        bind_ip=args.bind_ip,
        control_timeout_s=1.0,
    ).start()
    link.resume()
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and link.status != "DISARMED":
            link.set_sticks(roll=1500, pitch=1500, throttle=1000, yaw=1500)
            time.sleep(0.02)
        if link.status != "DISARMED":
            raise RuntimeError(f"ESP link did not enter DISARMED: {link.status}")

        link.set_sticks(roll=1500, pitch=1500, throttle=1000, yaw=1500)
        channels = read_msp_rc(args.port)
        if len(channels) < 6:
            raise RuntimeError(f"Betaflight returned only {len(channels)} RC channels")

        # MSP_RC returns Betaflight's internal RPYT order. This differs from
        # the AETR order of the incoming CRSF channel slots.
        names = ("roll", "pitch", "yaw", "throttle", "aux1", "aux2")
        for name, value in zip(names, channels[:6]):
            print(f"{name}={value}")

        expected = (1500, 1500, 1500, 1000, 1000, 2000)
        failures = [
            f"{name}: expected {target}, got {actual}"
            for name, actual, target in zip(names, channels[:6], expected)
            if abs(actual - target) > 15
        ]
        if failures:
            raise RuntimeError("Unexpected Betaflight channels: " + "; ".join(failures))
        print("Betaflight received the expected neutral, disarmed CRSF channels.")
        return 0
    finally:
        link.disarm()
        time.sleep(0.1)
        link.stop()


if __name__ == "__main__":
    raise SystemExit(main())
