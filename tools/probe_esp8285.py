"""Identify the ESP ROM through USB-controlled Betaflight UART passthrough.

Requires hardware receiver boot mode. Does not erase/write flash, upload a
stub, reset the receiver, or change saved Betaflight settings. Leaves the
passthrough active so a successful probe can be followed by a firmware update.
"""

import argparse
import time

import serial
from esptool import FatalError
from esptool.targets.esp8266 import ESP8266ROM

from backup_esp8285 import enable_passthrough


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--uart", default="UART3")
    parser.add_argument("--resume", action="store_true",
                        help="USB-controlled (baud 0) passthrough is already active")
    args = parser.parse_args()
    if not args.resume:
        enable_passthrough(args.port, args.uart, 0)

    for baud in (115200, 420000, 460800, 230400, 57600, 74880):
        print(f"Probing ROM at {baud} baud...", flush=True)
        with serial.Serial(args.port, baud, timeout=0.2, write_timeout=1) as port:
            esp = ESP8266ROM(port, baud=baud)
            time.sleep(0.1)
            try:
                esp.connect(mode="no_reset", attempts=1, warnings=False)
                magic = esp.read_reg(esp.CHIP_DETECT_MAGIC_REG_ADDR)
                if magic not in esp.CHIP_DETECT_MAGIC_VALUE:
                    raise RuntimeError(f"Unexpected chip signature: {magic:#x}")
                print(f"Chip: {esp.get_chip_description()}")
                print("MAC: " + ":".join(f"{value:02X}" for value in esp.read_mac()))
                print(f"ROM verified at {baud} baud. Flash remains unchanged.")
                return 0
            except FatalError as exc:
                print(str(exc).splitlines()[0])
    print("No valid ESP8266/ESP8285 ROM response at the tested rates. No flash was written.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
