"""Read and verify full ESP8285 flash backups through Betaflight.

This utility changes only volatile runtime state: it places Betaflight in serial
passthrough and asks a running ExpressLRS receiver to enter its ROM bootloader.
It invokes esptool's read_flash command and contains no erase or write command.
Make the two independent reads in separate power cycles, using --compare-with
for the second read. Power-cycle the flight controller after each read.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import serial


ESP_FLASH_SIZE = 0x100000
DEFAULT_PASSTHROUGH_BAUD = 420000
DEFAULT_ESPTOOL_BAUD = 115200


def crc8_dvb_s2(data: bytes) -> int:
    crc = 0
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = ((crc << 1) ^ 0xD5) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def receiver_bootloader_sequence() -> bytes:
    payload = bytes((0xEC, 0x04, 0x32, ord("b"), ord("l")))
    return payload + bytes((crc8_dvb_s2(payload[2:]),))


def read_available(port: serial.Serial, wait_s: float = 0.35) -> bytes:
    deadline = time.monotonic() + wait_s
    received = bytearray()
    while time.monotonic() < deadline:
        waiting = port.in_waiting
        if waiting:
            received.extend(port.read(waiting))
            deadline = time.monotonic() + 0.08
        else:
            time.sleep(0.01)
    return bytes(received)


def cli_command(port: serial.Serial, command: str) -> bytes:
    port.write(command.encode("ascii") + b"\r\n")
    port.flush()
    return read_available(port)


def enable_passthrough(com_port: str, uart: str, baud: int) -> None:
    print(f"Opening Betaflight CLI on {com_port}...")
    with serial.Serial(com_port, 115200, timeout=0.1, write_timeout=1) as port:
        port.reset_input_buffer()
        port.write(b"#")
        port.flush()
        prompt = read_available(port, 0.8)
        if b"#" not in prompt:
            raise RuntimeError("Betaflight CLI prompt was not detected; power-cycle and retry")

        checks = {
            "serialrx_provider": b"CRSF",
            "serialrx_inverted": b"OFF",
            "serialrx_halfduplex": b"OFF",
        }
        for setting, expected in checks.items():
            response = cli_command(port, f"get {setting}")
            if expected not in response:
                text = response.decode("ascii", errors="replace").strip()
                raise RuntimeError(f"Unexpected {setting}; expected {expected.decode()}: {text}")

        serial_config = cli_command(port, "serial")
        uart_line = next(
            (line for line in serial_config.splitlines() if line.startswith(f"serial {uart} ".encode())),
            None,
        )
        if uart_line is None:
            raise RuntimeError(f"{uart} was not found in Betaflight's serial configuration")
        fields = uart_line.split()
        if len(fields) < 3 or not (int(fields[2]) & 64):
            raise RuntimeError(f"Serial RX is not enabled on {uart}: {uart_line!r}")

        print(f"Enabling serial passthrough on {uart} at {baud} baud...")
        # A single terminator avoids forwarding a trailing LF into the ESP ROM
        # before esptool sends its baud-detection sequence.
        port.write(f"serialpassthrough {uart} {baud} rxtx\r".encode("ascii"))
        port.flush()
        response = read_available(port, 0.8)
        printable = response.decode("ascii", errors="replace").strip()
        print(printable)
        if b"Forwarding" not in response:
            raise RuntimeError("Betaflight did not confirm serial passthrough")
        if baud == 0 and b"baud rate change over USB enabled" not in response:
            raise RuntimeError("Betaflight did not enable USB-controlled UART baud rate")


def reboot_receiver_to_rom(com_port: str, baud: int) -> bytes:
    print("Requesting the running ExpressLRS receiver to enter its ROM bootloader...")
    with serial.Serial(com_port, baud, timeout=0.1, write_timeout=1) as port:
        port.reset_input_buffer()
        # This training sequence and CRSF command match the official ExpressLRS
        # Betaflight passthrough implementation.
        port.write(b"\x07\x07\x12\x20" + (b"\x55" * 32))
        port.flush()
        time.sleep(0.2)
        port.write(receiver_bootloader_sequence())
        port.flush()
        response = read_available(port, 0.8)
    time.sleep(0.5)
    return response


def read_flash(com_port: str, esptool_baud: int, output: Path) -> None:
    command = [
        sys.executable,
        "-m",
        "esptool",
        "--chip",
        "esp8266",
        "--port",
        com_port,
        "--baud",
        str(esptool_baud),
        "--before",
        "no_reset",
        "--after",
        "no_reset",
        "read_flash",
        "0",
        hex(ESP_FLASH_SIZE),
        str(output),
    ]
    subprocess.run(command, check=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, help="flight-controller port, such as COM4")
    parser.add_argument("--uart", default="UART3", help="Betaflight receiver UART (default: UART3)")
    parser.add_argument("--baud", type=int, default=DEFAULT_PASSTHROUGH_BAUD)
    parser.add_argument(
        "--esptool-baud",
        type=int,
        default=DEFAULT_ESPTOOL_BAUD,
        help="logical virtual-COM rate; keep 115200 to avoid a passthrough baud transition",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("backups"))
    parser.add_argument(
        "--resume",
        action="store_true",
        help="receiver is already in ROM bootloader behind an active passthrough",
    )
    parser.add_argument(
        "--compare-with",
        type=Path,
        help="first 1 MB backup to compare against this independent read",
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    suffix = "verify" if args.compare_with else "a"
    backup = args.output_dir / f"air75ii-esp8285-{stamp}-{suffix}.bin"
    if backup.exists():
        raise FileExistsError(f"Refusing to overwrite existing backup: {backup}")

    if args.compare_with:
        if not args.compare_with.is_file():
            raise FileNotFoundError(f"Comparison backup was not found: {args.compare_with}")
        if args.compare_with.stat().st_size != ESP_FLASH_SIZE:
            raise RuntimeError(f"Comparison backup is not exactly 1 MB: {args.compare_with}")

    if not args.resume:
        enable_passthrough(args.port, args.uart, args.baud)
        target_response = reboot_receiver_to_rom(args.port, args.baud)
        if target_response:
            printable = target_response.decode("ascii", errors="replace").strip()
            print(f"Receiver response: {printable}")

    print(f"Reading full flash to {backup}...")
    read_flash(args.port, args.esptool_baud, backup)

    backup_hash = sha256(backup)
    print(f"SHA-256: {backup_hash}")
    if args.compare_with:
        comparison_hash = sha256(args.compare_with)
        print(f"Comparison SHA-256: {comparison_hash}")
        if backup_hash != comparison_hash:
            raise RuntimeError("Backup hashes do not match; do not flash this receiver")
        hash_file = args.output_dir / f"air75ii-esp8285-{stamp}.sha256.txt"
        hash_file.write_text(
            f"{comparison_hash}  {args.compare_with.name}\n{backup_hash}  {backup.name}\n",
            encoding="ascii",
        )
        print(f"Verified matching backups. Hash record: {hash_file}")
    else:
        print("First backup complete. Power-cycle, then rerun with --compare-with and this file.")
    print("Power-cycle the flight controller to leave passthrough/bootloader mode.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
