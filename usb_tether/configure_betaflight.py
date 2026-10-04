"""Switch Betaflight's receiver between USB MSP (tether) and serial CRSF (Wi-Fi).

Read-only by default. With --apply it changes only the RX_SERIAL/RX_MSP
feature bits, saves to EEPROM, and reboots the flight controller. Every change
is appended to usb_tether/betaflight_feature_changes.txt.

  python usb_tether/configure_betaflight.py                 # show current mode
  python usb_tether/configure_betaflight.py --mode msp --apply
  python usb_tether/configure_betaflight.py --mode crsf --apply   # undo
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
from datetime import datetime
from pathlib import Path

import serial

sys.path.insert(0, str(Path(__file__).resolve().parent))
from msp import (FEATURE_RX_MSP, FEATURE_RX_SERIAL, MSP_API_VERSION, MSP_EEPROM_WRITE,
                 MSP_FEATURE_CONFIG, MSP_SET_FEATURE_CONFIG, MSP_SET_REBOOT, MSP_STATUS,
                 FrameParser, encode_request, find_flight_controller, parse_status)

LOG = Path(__file__).resolve().parent / "betaflight_feature_changes.txt"


def request(port: serial.Serial, command: int, payload: bytes = b"", timeout: float = 2.0) -> bytes:
    port.reset_input_buffer()
    port.write(encode_request(command, payload))
    parser = FrameParser()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for reply, data, ok in parser.feed(port.read(port.in_waiting or 1)):
            if reply == command:
                if not ok:
                    raise RuntimeError(f"Betaflight rejected MSP command {command}")
                return data
    raise TimeoutError(f"No reply to MSP command {command}")


def describe(features: int) -> str:
    serial_rx = bool(features & FEATURE_RX_SERIAL)
    msp_rx = bool(features & FEATURE_RX_MSP)
    if msp_rx and not serial_rx:
        return "MSP (USB tether)"
    if serial_rx and not msp_rx:
        return "SERIAL (CRSF from the ESP8285 Wi-Fi bridge)"
    return f"OTHER (RX_SERIAL={serial_rx:d}, RX_MSP={msp_rx:d})"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", help="flight-controller COM port (auto-detected)")
    parser.add_argument("--mode", choices=("msp", "crsf"))
    parser.add_argument("--apply", action="store_true", help="write, save and reboot")
    args = parser.parse_args()

    port_name = args.port or find_flight_controller()
    if not port_name:
        print("No Betaflight USB port found. Plug in the drone's USB cable.", file=sys.stderr)
        return 2

    with serial.Serial(port_name, 115200, timeout=0.05, write_timeout=1) as port:
        api = request(port, MSP_API_VERSION)
        features = struct.unpack("<I", request(port, MSP_FEATURE_CONFIG)[:4])[0]
        armed, _ = parse_status(request(port, MSP_STATUS))
        print(f"{port_name}: MSP API {api[1]}.{api[2]}, features 0x{features:08x}")
        print(f"Receiver: {describe(features)}")
        if not args.mode:
            return 0

        wanted = features & ~(FEATURE_RX_SERIAL | FEATURE_RX_MSP)
        wanted |= FEATURE_RX_MSP if args.mode == "msp" else FEATURE_RX_SERIAL
        if wanted == features:
            print("Already set; nothing to do.")
            return 0
        print(f"Change: 0x{features:08x} -> 0x{wanted:08x} ({describe(wanted)})")
        if not args.apply:
            print("Dry run. Add --apply to save and reboot the flight controller.")
            return 0
        if armed:
            print("Refusing: the flight controller is ARMED.", file=sys.stderr)
            return 1

        with LOG.open("a", encoding="utf-8") as log:
            log.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {port_name} "
                      f"features 0x{features:08x} -> 0x{wanted:08x} ({args.mode})\n")
        request(port, MSP_SET_FEATURE_CONFIG, struct.pack("<I", wanted))
        request(port, MSP_EEPROM_WRITE)
        port.write(encode_request(MSP_SET_REBOOT))
        port.flush()
    print("Saved; flight controller rebooting.")

    # The USB port disappears during reboot; wait for it, then verify.
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        time.sleep(1)
        port_name = args.port or find_flight_controller()
        if not port_name:
            continue
        try:
            with serial.Serial(port_name, 115200, timeout=0.05, write_timeout=1) as port:
                time.sleep(0.5)
                after = struct.unpack("<I", request(port, MSP_FEATURE_CONFIG)[:4])[0]
        except (serial.SerialException, OSError, TimeoutError):
            continue
        print(f"Verified after reboot: {describe(after)}")
        return 0 if after == wanted else 1
    print("Flight controller did not come back within 15 s; check it in Betaflight Configurator.",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
