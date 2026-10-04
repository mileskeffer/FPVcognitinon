"""Flash a prebuilt Air75 Wi-Fi bridge image through Betaflight passthrough."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

from backup_esp8285 import (
    DEFAULT_ESPTOOL_BAUD,
    DEFAULT_PASSTHROUGH_BAUD,
    enable_passthrough,
    reboot_receiver_to_rom,
)


MAX_IMAGE_SIZE = 958448


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def validate_image(path: Path, expected_hash: str) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"Firmware image was not found: {path}")
    size = path.stat().st_size
    if not 1 <= size <= MAX_IMAGE_SIZE:
        raise RuntimeError(f"Unexpected ESP8285 image size {size}: {path}")
    with path.open("rb") as file:
        if file.read(1) != b"\xE9":
            raise RuntimeError("Image does not begin with an ESP8266 executable header (0xE9)")
    actual_hash = sha256(path)
    if actual_hash != expected_hash.upper():
        raise RuntimeError(f"Image SHA-256 mismatch: expected {expected_hash}, got {actual_hash}")
    return actual_hash


def write_flash(com_port: str, esptool_baud: int, image: Path) -> None:
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
        "soft_reset",
        "write_flash",
        "--flash_mode",
        "dout",
        "--flash_freq",
        "40m",
        "--flash_size",
        "1MB",
        "0x0",
        str(image),
    ]
    subprocess.run(command, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, help="flight-controller port, such as COM4")
    parser.add_argument("--uart", default="UART3")
    parser.add_argument(
        "--baud",
        type=int,
        default=None,
        help=(
            "Betaflight UART passthrough rate; defaults to 420000 for a running "
            "ELRS receiver and 115200 for --receiver-in-rom"
        ),
    )
    parser.add_argument("--esptool-baud", type=int, default=DEFAULT_ESPTOOL_BAUD)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--sha256", required=True, help="required expected SHA-256 of the exact image")
    parser.add_argument(
        "--receiver-in-rom",
        action="store_true",
        help=(
            "the ESP8285 was placed in ROM download mode with its hardware "
            "boot button; enable FC passthrough but do not send the ELRS reboot command"
        ),
    )
    args = parser.parse_args()

    verified_hash = validate_image(args.image, args.sha256)
    print(f"Verified image: {args.image}", flush=True)
    print(f"Size: {args.image.stat().st_size} bytes", flush=True)
    print(f"SHA-256: {verified_hash}", flush=True)

    passthrough_baud = args.baud
    if passthrough_baud is None:
        passthrough_baud = (
            DEFAULT_ESPTOOL_BAUD if args.receiver_in_rom else DEFAULT_PASSTHROUGH_BAUD
        )
    enable_passthrough(args.port, args.uart, passthrough_baud)
    if args.receiver_in_rom:
        print("Receiver hardware boot mode selected; skipping the ELRS reboot command.", flush=True)
    else:
        target_response = reboot_receiver_to_rom(args.port, passthrough_baud)
        printable = target_response.decode("ascii", errors="replace").strip()
        if "UNIFIED_ESP8285_2400_RX" not in printable:
            raise RuntimeError(f"Unexpected receiver target response: {printable!r}")
        print(f"Receiver response: {printable}", flush=True)
    print("Writing and verifying the ESP8285 image...", flush=True)
    write_flash(args.port, args.esptool_baud, args.image)
    print("Flash verified. Power-cycle the flight controller before testing.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
