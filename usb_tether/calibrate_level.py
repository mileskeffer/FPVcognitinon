"""Recalibrate Betaflight's accelerometer ("level") with the drone sitting flat.

ANGLE mode holds whatever the accelerometer calls level. Flight logs on
2026-10-04 showed about +6 deg pitch while the drone sat on a flat surface, so
ANGLE mode tipped the nose instead of climbing and the rear motors stayed at
idle.

Before running: drone disarmed, props on or off, sitting upright on a flat,
level surface, perfectly still, with the tether cable slack so it does not
pull on the frame. Betaflight saves the calibration itself.

  python usb_tether/calibrate_level.py          # show current attitude only
  python usb_tether/calibrate_level.py --apply  # calibrate, then show result
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
from pathlib import Path

import serial

sys.path.insert(0, str(Path(__file__).resolve().parent))
from configure_betaflight import request
from msp import MSP_ATTITUDE, MSP_STATUS, find_flight_controller, parse_status

MSP_ACC_CALIBRATION = 205
MAX_TILT_DEG = 20.0
MAX_MOTION_DEG = 0.5


def read_attitude(port: serial.Serial, samples: int = 10) -> list[tuple[float, float]]:
    readings = []
    for _ in range(samples):
        roll, pitch = struct.unpack_from("<hh", request(port, MSP_ATTITUDE))
        readings.append((roll / 10, pitch / 10))
        time.sleep(0.05)
    return readings


def summary(readings: list[tuple[float, float]]) -> tuple[float, float, float]:
    rolls = [r for r, _ in readings]
    pitches = [p for _, p in readings]
    motion = max(max(rolls) - min(rolls), max(pitches) - min(pitches))
    return sum(rolls) / len(rolls), sum(pitches) / len(pitches), motion


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port")
    parser.add_argument("--apply", action="store_true", help="run accelerometer calibration")
    args = parser.parse_args()

    port_name = args.port or find_flight_controller()
    if not port_name:
        print("No Betaflight USB port found.", file=sys.stderr)
        return 2
    with serial.Serial(port_name, 115200, timeout=0.02, write_timeout=1) as port:
        roll, pitch, motion = summary(read_attitude(port))
        print(f"Current: roll {roll:+.1f}, pitch {pitch:+.1f} deg (movement {motion:.1f} deg)")
        if not args.apply:
            return 0
        if parse_status(request(port, MSP_STATUS))[0]:
            print("Refusing: the drone is ARMED.", file=sys.stderr)
            return 1
        if abs(roll) > MAX_TILT_DEG or abs(pitch) > MAX_TILT_DEG:
            print("Refusing: the drone is not upright. Sit it flat, right side up.",
                  file=sys.stderr)
            return 1
        if motion > MAX_MOTION_DEG:
            print("Refusing: the drone is moving. Leave it still and retry.", file=sys.stderr)
            return 1

        print("Calibrating; keep the drone still...")
        request(port, MSP_ACC_CALIBRATION)
        time.sleep(3.0)
        roll, pitch, motion = summary(read_attitude(port))
        print(f"After:   roll {roll:+.1f}, pitch {pitch:+.1f} deg")
        if abs(roll) > 1.0 or abs(pitch) > 1.0:
            print("Still more than 1 deg off level; check the surface and retry.", file=sys.stderr)
            return 1
    print("Level calibrated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
