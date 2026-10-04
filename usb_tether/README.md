# USB tether control

Fly the BETAFPV Air75 II from a Logitech F310 gamepad through a USB cable
plugged straight into the flight controller. No radio is involved, so the motor
interference that broke the [Wi-Fi approach](../wifi-2.4ghz/) does not apply.

```text
F310 gamepad -> PC (tether_control.py) -> USB cable -> Betaflight (MSP receiver) -> motors
```

The PC sends stick commands 50 times a second as Betaflight MSP messages. If
they stop (cable pulled, program closed), Betaflight's own failsafe cuts the
motors.

**Status:** the drone lifts off and flies, but needs constant correction. The
cable pulls on it, it has no position or height sensor, and small rooms create
rough air. See [limitations.txt](limitations.txt) before expecting a steady
hover.

## Files

| File | Purpose |
|---|---|
| `tether_control.py` | Gamepad app: arming, flight assist or manual control, safety cutoffs, flight logs |
| `flight_assist.py` | Assist mode: sticks request a speed, centred sticks brake to a stop |
| `msp.py` | USB link to Betaflight: sends controls, reads status, attitude, motors, accelerometer |
| `configure_betaflight.py` | Switch the receiver between USB (MSP) and the Wi-Fi bridge (CRSF) |
| `calibrate_level.py` | Recalibrate the accelerometer's idea of level |
| `tune_betaflight.py` | Change Betaflight settings (airmode, self-levelling, smoothing, any numeric setting) |
| `test_disarmed_tether.py` | Disarmed check: sends controls and reads them back from Betaflight |
| `analyze_flight.py` | Summarise a flight log: uncommanded tilts, throttle response, motor saturation |
| `betaflight_feature_changes.txt` | Log of every Betaflight change made by these tools |
| `limitations.txt` | Known problems with this approach |
| `tests/` | Unit and simulation tests |

All tools only read unless given `--apply`. Run commands from the repository
root with the project virtual environment.

## One-time setup

Props off, drone plugged in over USB.

```powershell
.\.venv\Scripts\python.exe usb_tether\configure_betaflight.py --mode msp --apply
.\.venv\Scripts\python.exe usb_tether\test_disarmed_tether.py      # must print PASS
```

Then sit the drone upright on a flat surface, still, with the cable slack:

```powershell
.\.venv\Scripts\python.exe usb_tether\calibrate_level.py --apply
```

To return to the Wi-Fi bridge later: `configure_betaflight.py --mode crsf --apply`.

### Betaflight settings currently applied

Changed from the stock configuration during testing on 2026-10-04:

| Setting | Stock | Now | Why |
|---|---|---|---|
| Receiver | Serial (CRSF) | MSP | Controls arrive over USB |
| `rc_smoothing_*_cutoff` | auto | 25 Hz | Auto never detected the USB frame rate and made throttle lag by seconds |
| `angle_p_gain` | 50 | 55 | Faster return to level |
| `i_pitch` / `i_roll` | 59 / 60 | 35 / 35 | Less stored correction to release when the cable lets go |
| `iterm_windup` | 80 | 50 | Same |
| `debug_mode` | NONE | EZLANDING | Logs the mixer throttle for diagnosis |
| Accelerometer | factory | recalibrated | Was about 6 deg off level |

AIRMODE is on (as stock). Exact history is in `betaflight_feature_changes.txt`.

## Flying

Props off for the first run. Plug in the battery, then:

```powershell
.\.venv\Scripts\python.exe usb_tether\tether_control.py
```

| Input | Assist mode (default) | Manual mode |
|---|---|---|
| Hold Start 3 s | Arm | Arm |
| Start (armed) or B | Disarm | Disarm |
| Back | Kill: stop sending, Betaflight failsafe | Same |
| Left stick | Requested speed; centred = brake and hold still | Tilt angle |
| Right stick X | Yaw | Yaw |
| Right trigger | Take off, then nudge up | Throttle (full trigger = 1500) |
| Left trigger | Nudge down; hold fully 1.5 s to land and disarm | Lower throttle |
| D-pad / X | Trim / reset trim | Same |
| Y (on the ground) | Switch to manual | Switch to assist |
| Esc | Quit | Quit |

Useful options: `--mode manual`, `--hover-guess 1340` (starting hover throttle
in assist mode; it auto-trims), `--max-speed`, `--assist-tilt`, `--max-tilt`.

### Safety behaviour

- Arming needs centred sticks, released triggers and a 3-second Start hold.
- If the drone tilts past 70 deg it disarms instead of tumbling on the cable
  (`--max-tilt`, 0 disables).
- If the gamepad, the app's main loop or the USB link stops, the app stops
  sending and Betaflight's failsafe drops the motors.
- Mode switching is refused in the air.

### Flying tips

- Keep the cable slack and hanging straight down from a point above the drone.
  If you have to hold a stick to stay in place, the cable is pulling.
- Hover at least 30 cm up and away from walls to reduce prop-wash turbulence.
- Bring the sticks back to centre before lowering the throttle.

## Logs and analysis

Every armed session saves `logs/tether-flight-*.json` (sticks sent, what
Betaflight received, attitude, motors, accelerometer and assist state at 20 Hz).
Faults save `logs/tether-fault-*.json`.

```powershell
.\.venv\Scripts\python.exe usb_tether\analyze_flight.py          # newest log
```

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s usb_tether/tests -t usb_tether
```

The flight-assist tests fly the controller against a simple simulated drone;
they do not prove real-flight behaviour.

## Lighter tether option

The board's UART1 pads (TX1, RX1, GND) accept the same MSP commands. Three thin
wires to a 3.3 V USB-serial adapter weigh far less than a USB-C cable. Enable
MSP on UART1 in Betaflight and run `tether_control.py --port COMx` with the
adapter's port. This has not been built or tested yet.
