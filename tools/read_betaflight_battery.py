"""Read Betaflight's battery voltage over MSP without changing configuration."""

from __future__ import annotations

import argparse
import struct
import time

import serial


MSP_ANALOG = 110
MSP_BATTERY_STATE = 130


def read_msp(com_port: str, command_requested: int) -> bytes:
    request = b"$M<" + bytes((0, command_requested, command_requested))
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
            checksum = length ^ command
            for value in payload:
                checksum ^= value
            if command != command_requested or checksum != data[start + 5 + length]:
                continue
            return payload
    raise TimeoutError(f"Betaflight did not return MSP command {command_requested} within two seconds")


def read_analog(com_port: str) -> tuple[float, int, float]:
    payload = read_msp(com_port, MSP_ANALOG)
    if len(payload) < 7:
        raise RuntimeError(f"MSP_ANALOG payload is too short: {len(payload)}")
    legacy_voltage, mah_drawn, _rssi, current = struct.unpack_from("<BHHh", payload)
    voltage = (
        struct.unpack_from("<H", payload, 7)[0] / 100.0
        if len(payload) >= 9
        else legacy_voltage / 10.0
    )
    return voltage, mah_drawn, current / 100.0


def read_battery_state(com_port: str) -> tuple[int, float]:
    payload = read_msp(com_port, MSP_BATTERY_STATE)
    if len(payload) < 9:
        raise RuntimeError(f"MSP_BATTERY_STATE payload is too short: {len(payload)}")
    cell_count = payload[0]
    voltage = (
        struct.unpack_from("<H", payload, 9)[0] / 100.0
        if len(payload) >= 11
        else payload[3] / 10.0
    )
    return cell_count, voltage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="COM4")
    args = parser.parse_args()
    voltage, mah_drawn, current = read_analog(args.port)
    cell_count, state_voltage = read_battery_state(args.port)
    print(f"battery_voltage={voltage:.2f} V")
    print(f"battery_state_voltage={state_voltage:.2f} V")
    print(f"cell_count={cell_count}")
    print(f"consumed={mah_drawn} mAh")
    print(f"current={current:.2f} A")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
