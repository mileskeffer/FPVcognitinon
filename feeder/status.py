import argparse

parser = argparse.ArgumentParser(
    description="Acquire/Send diagnositic ping information."
)

parser.add_argument("-t", "--targets", default="192.168.0.192,192.168.0.196", help="CSV string of target IPv4 addresses: ex. 192.168.0.192,192.168.0.196")
parser.add_argument("-n", "--names", default="pi-zero,my-laptop", help="CSV string of target names: ex. pi-zero,my-laptop")
args = parser.parse_args()

import ipaddress
import platform
import re
import subprocess
import sys

def get_ping_times(targets, names, count: int = 4, timeout_s: int = 2):
    ping_times = []
    for i, host in enumerate(targets):
        try:
            ipaddress.IPv4Address(host)  # raises ValueError on bad input
        except ValueError:
            print(f"[-] Invalid Address: {host} - {names[i]}")
            print(" |___ Skipping target...")
            continue

        print(f"[*] ({i+1}/{len(targets)}) Pinging: {host}...")

        # -W is per-reply timeout in seconds on Linux; macOS uses ms and a different flag meaning
        cmd = ["ping", "-c", str(count), "-W", str(timeout_s), host]

        try:
            out = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=4 * (timeout_s + 1) + 5,
            ).stdout
        except subprocess.TimeoutExpired:
            ping_times.append(None)

        print(out)

        # Linux/macOS: "rtt min/avg/max/mdev = 9.1/10.2/11.9/0.8 ms"
        m = re.search(r"= [\d.]+/([\d.]+)/[\d.]+/[\d.]+ ms", out)
        ping_times.append(float(m.group(1)) if m else None)
    return ping_times

import requests

def send_stats(times, names, PORT: int = 3000):
    print("[*] Attempting to deliver ping-times...")
    try:
        payload = []
        for i, t in enumerate(times):
            name = names[i]
            payload.append({"name": name, "time": t })

        url = f"http://localhost:{PORT}/api/stats"

        # Send the POST request with a 5-second timeout
        response = requests.post(url, json={ "ping_times": payload }, timeout=5)
        response.raise_for_status()

        # Parse the JSON response
        print(response.json())
    except Exception as e:
        print(e)

def main():
    global args
    targets = args.targets.split(',')
    print(f"[*] Targets: {targets}")

    names = args.names.split(',')
    print(f" |___ Names: {names}")

    ping_times = get_ping_times(targets, names)
    print(f"[*] Ping-Times: {ping_times}")

    send_stats(ping_times, names)

if __name__ == "__main__":
    main()
