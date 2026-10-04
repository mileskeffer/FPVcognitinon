# FPVcognitinon

Hackathon project for **HD:CR 2026**: process the camera feed from a small FPV
drone and run facial recognition on it, with the drone controlled from a PC.

The drone is a **BETAFPV Air75 II**, a 75 mm, 1S indoor "whoop" (about 20 g)
running Betaflight. It normally flies from an ExpressLRS radio handset. This
repository replaces that with control from a PC and a Logitech F310 gamepad,
so software can drive the drone.

## Two ways to control the drone

| | [Wi-Fi 2.4 GHz](wifi-2.4ghz/) | [USB tether](usb_tether/) |
|---|---|---|
| How | Custom firmware on the drone's ESP8285 chip turns it into a Wi-Fi access point; the PC sends controls over Wi-Fi | A USB cable plugs straight into the flight controller; the PC sends Betaflight MSP commands down it |
| Range | A few metres, free flight | Cable length |
| Status | **Blocked:** motor noise cuts the Wi-Fi link before the drone can lift off | **Flies**, but needs constant correction; the cable pulls on the drone |
| Limitations | [wifi-2.4ghz/limitations.txt](wifi-2.4ghz/limitations.txt) | [usb_tether/limitations.txt](usb_tether/limitations.txt) |

Both are controlled from the same gamepad layout ([control-scheme.txt](control-scheme.txt)),
and in both Betaflight does the actual stabilising.

### What neither approach can fix

The Air75 II has only a gyro and accelerometer. It knows which way is level,
but not its position or height, so it drifts and cannot hover in place on its
own. Drones that do (for example a DJI Tello) use a downward camera and a height
sensor. In a small room the drone's own downdraft also bounces off the floor and
walls and pushes it around.

## TALK ABOUT FACIAL RECOG HERE

xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

## Repository layout

```text
wifi-2.4ghz/        Wi-Fi control: PC apps, link, diagnostics, full setup guide
usb_tether/         USB tether control: gamepad app, flight assist, Betaflight tools
firmware/           ESP8285 Wi-Fi bridge firmware (PlatformIO)
tools/              ESP8285 backup, flashing, OTA update and Betaflight test scripts
tests/              Tests for the Wi-Fi link and tools
backups/            Firmware images, flash backups, Betaflight config, install records
control-scheme.txt  Gamepad layout
backupinstructions.txt, flashinginstructions.txt   ESP8285 backup and flashing steps
logs/               Flight and fault logs (not committed)
```

## Getting started

Requirements: Windows, Python 3.10+, a Logitech F310 (switch on the back set to
X), and the Air75 II. The Wi-Fi approach also needs a USB Wi-Fi adapter
(a TP-Link TL-WN725N was used).

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r wifi-2.4ghz\requirements.txt
```

Then follow the README of the approach you want. The USB tether is the one that
currently flies:

```powershell
.\.venv\Scripts\python.exe usb_tether\tether_control.py
```

## Safety

- Remove the propellers for every setup step and first test.
- The ESP8285 firmware (`firmware/`) is only for the drone's receiver chip.
  Never flash it to the flight controller or through Betaflight Configurator.
- Back up the receiver before replacing its firmware (`backupinstructions.txt`).
- Both apps arm only after a 3-second Start hold with centred sticks, and stop
  the motors through Betaflight's failsafe if the link, gamepad or app fails.
