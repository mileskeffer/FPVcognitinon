"""Install a manual-connect Air75 profile; does not switch Wi-Fi networks."""

import argparse
import re
import subprocess
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interface", default="GDS VCI ONLY")
    args = parser.parse_args()
    credentials = (Path(__file__).resolve().parents[1] /
                   "firmware/air75_wifi_bridge/include/wifi_credentials.h").read_text()
    values = {}
    for key in ("SSID", "PASSWORD"):
        match = re.search(r'AIR75_WIFI_' + key + r'\[\]\s*=\s*"([^"\\]+)"', credentials)
        if not match:
            raise ValueError(f"Cannot parse local Wi-Fi {key}; no profile installed")
        values[key] = escape(match.group(1))
    profile = f'''<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
<name>{values['SSID']}</name>
<SSIDConfig><SSID><name>{values['SSID']}</name></SSID></SSIDConfig>
<connectionType>ESS</connectionType><connectionMode>manual</connectionMode>
<MSM><security><authEncryption><authentication>WPA2PSK</authentication>
<encryption>AES</encryption><useOneX>false</useOneX></authEncryption>
<sharedKey><keyType>passPhrase</keyType><protected>false</protected>
<keyMaterial>{values['PASSWORD']}</keyMaterial></sharedKey></security></MSM>
</WLANProfile>'''
    with tempfile.TemporaryDirectory(prefix="air75-profile-") as directory:
        path = Path(directory) / "profile.xml"
        path.write_text(profile, encoding="utf-8")
        subprocess.run(["netsh", "wlan", "add", "profile", f"filename={path}",
                        f"interface={args.interface}", "user=current"], check=True)
    print("Temporary credentials file removed. No network connection was changed.")


if __name__ == "__main__":
    main()
