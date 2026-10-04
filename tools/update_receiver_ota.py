"""Authenticated OTA upload of a checksum-verified ESP8285 image."""

import argparse
import base64
import http.client
import re
import secrets
from pathlib import Path

from flash_esp8285 import validate_image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, type=Path)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    validate_image(args.image, args.sha256)
    credentials = Path(__file__).resolve().parents[1] / "firmware/air75_wifi_bridge/include/wifi_credentials.h"
    match = re.search(r'AIR75_WIFI_PASSWORD\[\]\s*=\s*"([^"\\]+)"', credentials.read_text())
    if not match:
        raise ValueError("Cannot parse local Wi-Fi credentials; no upload attempted")
    authorization = "Basic " + base64.b64encode(("air75:" + match.group(1)).encode()).decode()
    headers = {"Authorization": authorization}
    connection = http.client.HTTPConnection("192.168.4.1", timeout=30)
    try:
        connection.request("GET", "/update", headers=headers)
        response = connection.getresponse()
        page = response.read()
        if response.status != 200 or b"firmware" not in page:
            raise RuntimeError(f"Maintenance preflight failed: HTTP {response.status}")
        boundary = "air75-" + secrets.token_hex(16)
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="firmware"; filename="firmware.bin"\r\n'
                'Content-Type: application/octet-stream\r\n\r\n').encode()
        body += args.image.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        headers["Content-Type"] = "multipart/form-data; boundary=" + boundary
        print(f"Uploading {args.image.stat().st_size} verified bytes", flush=True)
        connection.request("POST", "/update", body=body, headers=headers)
        response = connection.getresponse()
        result = response.read().decode(errors="replace")
        print(f"HTTP {response.status}: {result}")
        if response.status != 200 or "Update Success!" not in result:
            raise RuntimeError("Receiver did not confirm successful OTA update")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
