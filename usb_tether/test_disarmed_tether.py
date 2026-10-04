"""Disarmed-only smoke test: stream neutral RC over USB MSP and read it back.

Never sets the ARM channel. Checks that Betaflight receives the frames
(RXLOSS clears) and that the channels it reports match what was sent.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from msp import MspLink, describe_disable_flags, find_flight_controller


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port")
    parser.add_argument("--duration", type=float, default=3.0)
    args = parser.parse_args()
    port = args.port or find_flight_controller()
    if not port:
        print("No Betaflight USB port found.", file=sys.stderr)
        return 2

    with MspLink(port) as link:
        deadline = time.monotonic() + args.duration
        while time.monotonic() < deadline:
            # Refresh only neutral, minimum-throttle, explicitly disarmed data.
            link.set_sticks(roll=1600, pitch=1400, throttle=1000, yaw=1550)
            time.sleep(0.02)
        with link._lock:
            sent = link.channels_locked()
        # MSP_RC reports Betaflight's internal order: roll, pitch, yaw, throttle.
        expected = [sent[0], sent[1], sent[3], sent[2], *sent[4:]]
        received = link.fc_channels[:len(sent)]
        flags = link.arming_disable_flags
        print(f"state={link.status} link_ok={link.link_ok} rc_acks={link.rc_acks} "
              f"frames_sent={link.sequence}")
        print(f"expected {expected}  (internal R,P,Y,T order)")
        print(f"received {list(received)}")
        print(f"arming blocked by: {describe_disable_flags(flags)}")
        ok = link.link_ok and list(received) == expected and not flags & (1 << 2)
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
