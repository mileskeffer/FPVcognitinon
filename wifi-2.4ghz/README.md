# Wi-Fi 2.4 GHz control

Fly the BETAFPV Air75 II from a PC gamepad over Wi-Fi, using custom firmware on
the drone's ESP8285 chip (normally its ExpressLRS receiver).

```text
F310 gamepad -> PC -> USB Wi-Fi adapter -> 2.4 GHz Wi-Fi -> ESP8285 (custom firmware)
                                                               |  CRSF serial
                                                     Betaflight flight controller -> motors
```

**Status: blocked.** The link is solid with the motors off, but motor and ESC
noise cuts it out once the motors spin up, before they reach enough power to
lift off. Development moved to the [USB tether](../usb_tether/). See
[limitations.txt](limitations.txt) for the details.

## Files

| File | Purpose |
|---|---|
| `gamepad_control.py` | Gamepad controller (pygame) with arming, kill and fault logging |
| `dronelink.py` | UDP control link to the drone: 28-byte packets with session, sequence and CRC |
| `windows_wifi.py` | Asks Windows to pause background Wi-Fi scans while flying |
| `receiver_diagnostics.py` | Reads the firmware's timing and fault record (read-only) |
| `web_control.py`, `control.html` | Optional browser controller |
| `mediamtx.yml` | Optional relay for a separate analog video capture |
| `AIR75_WIFI_DRONE.md` | Full setup guide: hardware checks, flashing, Betaflight, protocol |
| `limitations.txt` | Known problems with this approach |

Related folders in the repository root:

| Folder | Purpose |
|---|---|
| `../firmware/air75_wifi_bridge/` | ESP8285 firmware (PlatformIO) |
| `../tools/` | Backup, flashing, OTA update and link-test scripts |
| `../backups/` | Firmware images, flash backups and install records |

The firmware `.bin` is for the ESP8285 only. Never flash it to the STM32
flight controller or upload it through Betaflight Configurator.

## Installed firmware

`air75-diag-3`: Wi-Fi access point `Air75-Control` on channel 6, default
802.11 rates, 250 ms link-loss failsafe, and a diagnostic record of packet gaps
and the first failsafe cause. It sets the 802.11 mode explicitly because an
earlier 802.11b-only test build left that setting stored on the chip.

Firmware updates go over Wi-Fi while the drone is idle (no controller running):

```powershell
.\.venv\Scripts\python.exe tools\update_receiver_ota.py --image backups\<image>.bin --sha256 <hash>
```

## Running it

Betaflight must be set to the serial (CRSF) receiver. If the USB tether was
used last, switch back first:

```powershell
.\.venv\Scripts\python.exe usb_tether\configure_betaflight.py --mode crsf --apply
```

Props off. Connect the USB Wi-Fi adapter to `Air75-Control`, wait until Windows
shows it connected, then:

```powershell
.\.venv\Scripts\python.exe wifi-2.4ghz\gamepad_control.py
```

The window shows `DISARMED` in green once the drone answers; red `NO LINK` means
the PC is not connected to `Air75-Control` yet. Controls are listed in
`../control-scheme.txt`.

If `Air75-Control` does not appear in a Wi-Fi scan, the ESP8285 is not running
its access point; restarting the PC program will not help. Reconnect the
battery and check again.

## Faults and diagnostics

When the link drops, the controller stops, sends disarm, and saves
`logs/gamepad-fault-*.json` with PC-side timing plus the receiver's own record
before and after the fault. Receiver fault cause `1` is an armed packet timeout;
`2` is an explicit kill. To read the receiver record manually after closing the
controller:

```powershell
.\.venv\Scripts\python.exe wifi-2.4ghz\receiver_diagnostics.py
```

## Known issue: no self-levelling

The firmware sends AUX2 = 2000, but this drone's Betaflight setup enables ANGLE
(self-levelling) on AUX2 900-1300. Wi-Fi flights therefore ran in ACRO mode.
In `../firmware/air75_wifi_bridge/src/main.cpp`, change the two
`channelsUs[CHANNEL_ANGLE] = 2000` assignments (disarmed and armed) to 1000
before any further Wi-Fi testing.
