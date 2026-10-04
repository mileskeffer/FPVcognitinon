"""Find uncommanded tilts and motor saturation in a tether flight log.

  python usb_tether/analyze_flight.py                    # newest logs/tether-*.json
  python usb_tether/analyze_flight.py logs/tether-flight-....json

An "uncommanded tilt" is an armed sample where roll and pitch sticks are near
centre but Betaflight reports the craft tilted more than --tilt degrees. With
centred sticks ANGLE mode should hold level, so each one points at something
other than pilot input: cable pull, prop wash off walls/floor, an accelerometer
trim error, or a motor/prop problem (see the motor spread column).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CENTRE_US = 25


def load(path: Path | None) -> tuple[Path, list[dict]]:
    if path is None:
        candidates = sorted((ROOT / "logs").glob("tether-*.json"), key=lambda p: p.stat().st_mtime)
        candidates = [p for p in candidates if json.loads(p.read_text()).get("flight")]
        if not candidates:
            sys.exit("No tether log with flight samples in logs/. Fly once with the new recorder.")
        path = candidates[-1]
    return path, json.loads(path.read_text())["flight"]


def describe(sample: dict, t0: float) -> str:
    roll, pitch, throttle, yaw, _ = sample["sent"]
    att = sample["att"] or [math.nan, math.nan, math.nan]
    motors = sample["motors"] or []
    spread = (max(motors) - min(motors)) if motors else 0
    return (f"{sample['t'] - t0:7.2f}s  sticks R{roll:5} P{pitch:5} T{throttle:5} Y{yaw:5}  "
            f"att R{att[0]:6.1f} P{att[1]:6.1f} H{att[2]:4.0f}  "
            f"motors {' '.join(f'{m:4}' for m in motors)}  spread {spread:4}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("log", nargs="?", type=Path)
    parser.add_argument("--tilt", type=float, default=12.0, help="degrees (default 12)")
    args = parser.parse_args()

    path, samples = load(args.log)
    armed = [s for s in samples if s["state"] == "ARMED" and s["att"]]
    print(f"{path.name}: {len(samples)} samples, {len(armed)} armed with attitude")
    if not armed:
        return 0
    t0 = armed[0]["t"]

    events, current = [], []
    for sample in armed:
        roll, pitch = sample["sent"][0], sample["sent"][1]
        centred = abs(roll - 1500) <= CENTRE_US and abs(pitch - 1500) <= CENTRE_US
        tilt = math.hypot(sample["att"][0], sample["att"][1])
        if centred and tilt > args.tilt:
            current.append(sample)
        elif current:
            events.append(current)
            current = []
    if current:
        events.append(current)

    tilts = [math.hypot(s["att"][0], s["att"][1]) for s in armed]
    level = [s for s in armed if abs(s["sent"][0] - 1500) <= CENTRE_US
             and abs(s["sent"][1] - 1500) <= CENTRE_US and s["sent"][2] > 1100]
    print(f"Armed {armed[-1]['t'] - t0:.1f}s, max tilt {max(tilts):.1f} deg")
    if level:
        mean_roll = sum(s["att"][0] for s in level) / len(level)
        mean_pitch = sum(s["att"][1] for s in level) / len(level)
        print(f"Average attitude with sticks centred and throttle up: roll {mean_roll:+.1f}, "
              f"pitch {mean_pitch:+.1f} deg (a steady offset of several degrees suggests "
              "accelerometer trim or a constant cable pull)")
    high = [s for s in armed if s["sent"][2] >= 1800 and s["motors"]]
    if high:
        mean_motor = sum(sum(s["motors"]) / len(s["motors"]) for s in high) / len(high)
        print(f"Throttle >= 1800 sent in {len(high)} samples; average motor output "
              f"{mean_motor:.0f} (well under ~1600 means Betaflight is not following "
              "throttle, e.g. RC smoothing)")
        received = [s["rc"][3] for s in high if len(s.get("rc", [])) >= 4]
        if received:
            low = sum(1 for value in received if value < 1700)
            print(f"  Betaflight received throttle: min {min(received)}, max {max(received)}; "
                  f"{low} of {len(received)} samples below 1700 (signal loss / failsafe?)")
        mixer = [s["debug"][1] for s in high if len(s.get("debug", [])) >= 2]
        if mixer:
            print(f"  debug[1] (mixer throttle x10000 with debug_mode EZLANDING): "
                  f"min {min(mixer)}, max {max(mixer)}, avg {sum(mixer) / len(mixer):.0f}")
        flagged = [s["flags"] for s in high if s.get("flags")]
        if flagged:
            from msp import describe_disable_flags
            print(f"  Arming-disable flags seen while armed: "
                  f"{sorted({describe_disable_flags(f) for f in flagged})}")
    saturated = [s for s in armed if s["motors"] and max(s["motors"]) >= 1950]
    if saturated:
        print(f"Motor at or near 100% in {len(saturated)} samples (out of authority).")

    print(f"\n{len(events)} uncommanded tilt event(s) over {args.tilt:.0f} deg with sticks centred:")
    index = {id(s): i for i, s in enumerate(armed)}
    for event in events:
        start = index[id(event[0])]
        worst = max(event, key=lambda s: math.hypot(s["att"][0], s["att"][1]))
        print(f"\n-- {event[0]['t'] - t0:.2f}s, {len(event)} samples, "
              f"peak {math.hypot(worst['att'][0], worst['att'][1]):.1f} deg")
        for sample in armed[max(0, start - 10):start + len(event) + 3]:
            marker = ">" if sample in event else " "
            print(marker + describe(sample, t0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
