"""Tether tuning: turn off AIRMODE and soften ANGLE-mode self-levelling.

Why: on a tether the cable pulls the drone off level. With AIRMODE on,
Betaflight keeps full PID authority at idle throttle, so the I-term winds up
fighting the cable and drives some motors hard after the throttle is released.
A lower angle P gain also makes self-levelling push back less hard against
small cable forces.

Read-only by default (prints current values). With --apply it runs
``feature -AIRMODE`` / ``set <angle gain> = N`` / ``save`` in the Betaflight
CLI, which reboots the flight controller. Changes are appended to
usb_tether/betaflight_feature_changes.txt. Undo with --airmode on and the old
angle value printed by the read-only run.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import serial

sys.path.insert(0, str(Path(__file__).resolve().parent))
from msp import find_flight_controller

LOG = Path(__file__).resolve().parent / "betaflight_feature_changes.txt"
# Betaflight 4.5+ name first, older names after.
ANGLE_GAIN_NAMES = ("angle_p_gain", "angle_strength", "p_level")
RC_CUTOFF_NAMES = ("rc_smoothing_setpoint_cutoff", "rc_smoothing_throttle_cutoff")


def read_until_quiet(port: serial.Serial, wait_s: float = 0.4) -> str:
    deadline = time.monotonic() + wait_s
    received = bytearray()
    while time.monotonic() < deadline:
        waiting = port.in_waiting
        if waiting:
            received.extend(port.read(waiting))
            deadline = time.monotonic() + 0.1
        else:
            time.sleep(0.01)
    return received.decode("ascii", errors="replace")


def cli(port: serial.Serial, command: str, wait_s: float = 0.4) -> str:
    port.write(command.encode("ascii") + b"\r\n")
    port.flush()
    return read_until_quiet(port, wait_s)


def cli_exit(port: serial.Serial) -> None:
    """Leave the CLI; Betaflight reboots and the USB port vanishes at once."""
    try:
        port.write(b"exit\r\n")
        port.flush()
    except (serial.SerialException, OSError):
        pass


def parse_setting(output: str, name: str) -> int | None:
    match = re.search(rf"^{re.escape(name)}\s*=\s*(-?\d+)", output, re.MULTILINE)
    return int(match.group(1)) if match else None


def airmode_enabled(feature_output: str) -> bool:
    match = re.search(r"Enabled:\s*(.*)", feature_output)
    return bool(match and re.search(r"\bAIRMODE\b", match.group(1)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port")
    parser.add_argument("--airmode", choices=("on", "off"), help="default: unchanged")
    parser.add_argument("--angle-gain", type=int, help="new angle P gain (default: unchanged)")
    parser.add_argument("--rc-cutoff", type=int,
                        help="fixed RC smoothing cutoff in Hz for sticks and throttle; 0 = auto. "
                             "Auto needs a steady RX frame rate, which USB MSP from Windows "
                             "does not provide (default: unchanged)")
    parser.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                        help="any other numeric CLI setting, e.g. --set i_pitch=35 (repeatable)")
    parser.add_argument("--apply", action="store_true", help="save and reboot")
    args = parser.parse_args()

    port_name = args.port or find_flight_controller()
    if not port_name:
        print("No Betaflight USB port found. Plug in the tether cable.", file=sys.stderr)
        return 2

    with serial.Serial(port_name, 115200, timeout=0.1, write_timeout=1) as port:
        port.reset_input_buffer()
        port.write(b"#")
        port.flush()
        if "#" not in read_until_quiet(port, 0.8):
            print("Betaflight CLI prompt not detected. Close other programs using "
                  f"{port_name} (tether_control, Betaflight Configurator).", file=sys.stderr)
            return 1

        features = cli(port, "feature")
        airmode = airmode_enabled(features)
        angle_name = angle_value = None
        for name in ANGLE_GAIN_NAMES:
            value = parse_setting(cli(port, f"get {name}"), name)
            if value is not None:
                angle_name, angle_value = name, value
                break

        cutoffs = {name: parse_setting(cli(port, f"get {name}"), name) for name in RC_CUTOFF_NAMES}
        extra = {}
        for item in args.set:
            name, _, value = item.partition("=")
            name = name.strip()
            current = parse_setting(cli(port, f"get {name}"), name)
            if current is None or not value.strip().lstrip("-").isdigit():
                cli_exit(port)
                print(f"Unknown setting or non-numeric value: {item}; nothing changed.",
                      file=sys.stderr)
                return 1
            extra[name] = (current, int(value))
            print(f"{name} = {current}")
        print(f"{port_name}: AIRMODE {'ON' if airmode else 'OFF'}, "
              f"{angle_name or 'angle gain (not found)'} = {angle_value}, "
              + ", ".join(f"{name} = {value}" for name, value in cutoffs.items()))
        if angle_name is None:
            cli_exit(port)
            print("Could not find the angle gain setting; nothing changed.", file=sys.stderr)
            return 1

        new_angle = args.angle_gain if args.angle_gain is not None else angle_value
        want_airmode = airmode if args.airmode is None else args.airmode == "on"
        commands = []
        if airmode != want_airmode:
            commands.append(f"feature {'' if want_airmode else '-'}AIRMODE")
        if new_angle != angle_value:
            commands.append(f"set {angle_name} = {new_angle}")
        if args.rc_cutoff is not None:
            commands += [f"set {name} = {args.rc_cutoff}" for name, value in cutoffs.items()
                         if value != args.rc_cutoff]
        commands += [f"set {name} = {new}" for name, (old, new) in extra.items() if old != new]
        if not commands:
            cli_exit(port)
            print("Already set; nothing to do.")
            return 0
        print("Planned: " + "; ".join(commands))
        if not args.apply:
            cli_exit(port)
            print("Dry run (flight controller reboots on CLI exit). Add --apply to save.")
            return 0

        for command in commands:
            reply = cli(port, command)
            if "Invalid" in reply or "ERROR" in reply.upper():
                cli_exit(port)
                print(f"Betaflight rejected '{command}': {reply.strip()}\nNothing saved.",
                      file=sys.stderr)
                return 1
        with LOG.open("a", encoding="utf-8") as log:
            log.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {port_name} was: AIRMODE "
                      f"{'ON' if airmode else 'OFF'}, {angle_name} {angle_value}, "
                      + ", ".join(f"{name} {value}" for name, value in cutoffs.items())
                      + "".join(f", {name} {old}" for name, (old, _) in extra.items())
                      + "; ran: " + "; ".join(commands) + "\n")
        try:
            cli(port, "save", 1.0)
        except (serial.SerialException, OSError):
            pass  # save reboots the flight controller and closes the port
    print("Saved; flight controller rebooting.")

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        time.sleep(1)
        port_name = args.port or find_flight_controller()
        if not port_name:
            continue
        try:
            with serial.Serial(port_name, 115200, timeout=0.1, write_timeout=1) as port:
                time.sleep(0.5)
                port.reset_input_buffer()
                port.write(b"#")
                port.flush()
                read_until_quiet(port, 0.8)
                airmode = airmode_enabled(cli(port, "feature"))
                angle_value = parse_setting(cli(port, f"get {angle_name}"), angle_name)
                cutoffs = {name: parse_setting(cli(port, f"get {name}"), name)
                           for name in RC_CUTOFF_NAMES}
                extra_after = {name: parse_setting(cli(port, f"get {name}"), name)
                               for name in extra}
                cli_exit(port)
        except (serial.SerialException, OSError):
            continue
        print(f"Verified after reboot: AIRMODE {'ON' if airmode else 'OFF'}, "
              f"{angle_name} = {angle_value}, "
              + ", ".join(f"{name} = {value}" for name, value in cutoffs.items())
              + "".join(f", {name} = {value}" for name, value in extra_after.items()))
        cutoffs_ok = args.rc_cutoff is None or all(v == args.rc_cutoff for v in cutoffs.values())
        extra_ok = all(extra_after[name] == new for name, (_, new) in extra.items())
        ok = airmode == want_airmode and angle_value == new_angle and cutoffs_ok and extra_ok
        return 0 if ok else 1
    print("Flight controller did not come back within 15 s.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
