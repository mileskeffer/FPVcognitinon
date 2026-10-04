"""Run a short, disarmed-only smoke test against the Air75 Wi-Fi bridge."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dronelink import DroneLink


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind-ip", default="192.168.4.2")
    parser.add_argument("--drone-ip", default="192.168.4.1")
    parser.add_argument("--duration", type=float, default=3.0)
    args = parser.parse_args()

    link = DroneLink(ip=args.drone_ip, bind_ip=args.bind_ip).start()
    link.resume()
    deadline = time.monotonic() + args.duration
    try:
        while time.monotonic() < deadline:
            # Refresh only neutral, minimum-throttle, explicitly disarmed data.
            link.set_sticks(roll=1500, pitch=1500, throttle=1000, yaw=1500)
            time.sleep(0.02)
        print(f"state={link.status}")
        print(f"link_ok={link.link_ok}")
        print(f"last_ack_sequence={link.last_ack_sequence}")
        if link.status != "DISARMED" or not link.link_ok or link.last_ack_sequence == 0:
            return 1
        return 0
    finally:
        link.disarm()
        time.sleep(0.1)
        link.stop()


if __name__ == "__main__":
    raise SystemExit(main())
