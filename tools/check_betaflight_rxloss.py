"""Confirm Betaflight reports RXLOSS after Wi-Fi control packets stop."""

from __future__ import annotations

import argparse
import time

import serial


def read_until_idle(port: serial.Serial, wait_s: float = 0.5) -> bytes:
    deadline = time.monotonic() + wait_s
    data = bytearray()
    while time.monotonic() < deadline:
        waiting = port.in_waiting
        if waiting:
            data.extend(port.read(waiting))
            deadline = time.monotonic() + 0.1
        else:
            time.sleep(0.01)
    return bytes(data)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="COM4")
    args = parser.parse_args()

    time.sleep(0.5)
    with serial.Serial(args.port, 115200, timeout=0.1, write_timeout=1) as port:
        port.reset_input_buffer()
        port.write(b"#")
        port.flush()
        prompt = read_until_idle(port, 0.8)
        if b"#" not in prompt:
            raise RuntimeError("Betaflight CLI prompt was not detected")
        port.write(b"status\r\n")
        port.flush()
        response = read_until_idle(port, 0.8).decode("ascii", errors="replace")
        status_lines = [
            line.strip()
            for line in response.splitlines()
            if "arming disable" in line.lower() or "rx rate" in line.lower()
        ]
        for line in status_lines:
            print(line)
        found_rxloss = "RXLOSS" in response.upper()
        port.write(b"exit\r\n")
        port.flush()

    if not found_rxloss:
        raise RuntimeError("Betaflight did not report RXLOSS after packet output stopped")
    print("Betaflight RXLOSS confirmed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
