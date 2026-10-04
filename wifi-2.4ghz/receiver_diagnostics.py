"""Read-only receiver diagnostics; never sends control or arm packets."""

import argparse
import json
import socket
import time

# Both builds share the same diagnostic JSON layout.
SUPPORTED_FIRMWARE = ("air75-diag-1", "air75-diag-2", "air75-diag-3")


def fetch_diagnostics(ip="192.168.4.1", timeout=2.0):
    deadline = time.monotonic() + timeout
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((ip, 4211))
        while time.monotonic() < deadline:
            sock.settimeout(min(0.3, max(0.001, deadline - time.monotonic())))
            sock.send(b"A75D")
            try:
                data = json.loads(sock.recv(4096))
            except socket.timeout:
                continue
            if not isinstance(data, dict) or data.get("firmware") not in SUPPORTED_FIRMWARE:
                raise ValueError("Unexpected receiver diagnostic response")
            return data
    raise TimeoutError("Receiver diagnostics unavailable (active control, old firmware, or no link)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drone-ip", default="192.168.4.1")
    args = parser.parse_args()
    print(json.dumps(fetch_diagnostics(args.drone_ip), indent=2))
