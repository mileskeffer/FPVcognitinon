# Air75 II Wi-Fi gamepad control

This project sends PC gamepad input through a TP-Link TL-WN725N to the ESP8285
receiver processor on a BETAFPV Air75 II. The ESP8285 validates UDP control
packets and emits standard CRSF RC-channel frames to the STM32 flight
controller. Betaflight remains responsible for stabilization, arming checks,
motor output, and flight-controller failsafe.

```text
gamepad -> PC -> TL-WN725N -> 2.4 GHz Wi-Fi -> ESP8285
                                                  |
                                           CRSF, 420000 baud
                                                  |
                                         STM32 / Betaflight -> motors
```

The TL-WN725N is an ordinary Wi-Fi client adapter. Monitor mode, packet
injection, and a custom TP-Link driver are not required.

## Scope and hardware assumptions

This targets the Air75 II with the **Matrix 1S 5IN1 II** board and its onboard
**serial ELRS 2.4 GHz receiver**. The receiver implementation used by BETAFPV's
AIO ELRS target is an ESP8285 driving an SX1280-family radio. This firmware uses
the ESP8285's own 802.11 radio and no longer uses the SX1280 ELRS link.

Before flashing, physically confirm that your board revision has an ESP8285 and
locate the `RX-BOOT` pad. Stop if the receiver processor or board revision is
different.

This firmware is for the **ESP8285 receiver processor only**. Never upload its
`.bin` in Betaflight Configurator and never flash it to the STM32G473 flight
controller. Some Air75 II gyro revisions require BETAFPV's customized
Betaflight build.

Use `backupinstructions.txt` before changing the receiver. Then follow
`flashinginstructions.txt` for the first build, installation, recovery checks,
and props-off acceptance tests.

## Files

```text
firmware/air75_wifi_bridge/
  platformio.ini             reproducible ESP8285 build settings
  src/main.cpp               Wi-Fi-to-CRSF firmware
dronelink.py                 binary UDP link used by PC applications
gamepad_control.py            direct pygame gamepad controller
web_control.py                optional browser controller
control.html                  optional browser UI
mediamtx.yml                  optional analog-video capture relay
tests/test_protocol.py        PC packet-format tests
```

## Safety behavior

- Arming requires a disarmed packet first, throttle at or below 1050, and then
  a new arm request.
- Every packet carries a random process session, monotonic sequence number, and
  CRC-32. Old, malformed, wrong-session, and wrong-source packets are ignored.
- A different PC process cannot take over while the receiver considers the
  craft armed.
- If valid packets stop for 250 ms while armed, the ESP replaces the last
  command with centered sticks, minimum throttle, and ARM low. It transmits
  those disarmed channels for 500 ms before stopping CRSF. Betaflight then sees
  RX loss and executes its configured failsafe.
- A separate PC-side watchdog stops UDP output if the gamepad/UI loop freezes
  while its background network thread remains alive.
- After armed link loss, the ESP latches. It will not produce valid RC frames
  again until it receives a disarmed request.
- Emergency kill sends a short burst of disarmed CRSF frames and stops the
  control link.

Betaflight recommends flight-controller failsafe based on missing receiver
packets. Continuing to send made-up landing controls would hide link loss from
Betaflight, so the earlier receiver-side descent logic has been removed.

## 1. Preserve the stock configuration

Remove the propellers.

1. Connect with the BETAFPV-supported Betaflight Configurator version.
2. In the CLI, run `diff all` and save the complete output.
3. Record the board target, Betaflight version, gyro model, Ports settings,
   receiver settings, Modes, and Failsafe settings.
4. In the ExpressLRS Web UI, record/export the receiver hardware layout and
   firmware version if those controls are available.
5. Confirm that you can identify the receiver boot pad and the correct stock
   target, normally `BETAFPV 2.4GHz AIO RX`, before replacing ELRS.

Once this custom image boots, the ExpressLRS Web UI is gone. Restoring ELRS may
require grounding `RX-BOOT` during power-up and flashing through the receiver
UART/Betaflight passthrough. Do not use the aircraft as the first test platform
if you do not have the tools and soldering ability to perform that recovery.

## 2. Build the ESP8285 firmware

Install VS Code with PlatformIO, or install PlatformIO Core. From the project
root:

```bash
cd firmware/air75_wifi_bridge
pio run
```

The initial image is:

```text
.pio/build/air75_esp8285/firmware.bin
```

The build is pinned to the PlatformIO ESP8266 platform and selects:

- Generic ESP8285, 1 MB flash
- 80 MHz CPU and 40 MHz flash
- DOUT flash mode
- `eagle.flash.1m64.ld`

Before building, change `AP_PASSWORD` in `src/main.cpp`. Keep it at least eight
characters. The firmware SSID defaults to `Air75-Control`, channel 6, with one
associated Wi-Fi station allowed.

The binary must fit in the free OTA space reported by the currently installed
ELRS firmware. A successful local build alone does not prove that the existing
ELRS image has enough adjacent flash space for an OTA replacement.

## 3. Initial flash through the ELRS Web UI

These steps replace the ELRS firmware on the ESP8285. They do not change
Betaflight.

1. Power the quad over USB with props removed and wait for the receiver's
   `ExpressLRS RX` Wi-Fi network.
2. Join it using the ELRS Wi-Fi password, normally `expresslrs`.
3. Open `http://10.0.0.1` and use the receiver firmware update page.
4. Select `.pio/build/air75_esp8285/firmware.bin`.
5. A target mismatch is expected because this is not an ELRS build. Proceed
   only after confirming the upload is going to the ESP8285 receiver updater,
   the board is the expected BETAFPV AIO target, and boot-pad recovery is
   available.
6. Wait for the update and reboot to finish without removing power.
7. Look for `Air75-Control`.

If the updater reports insufficient space or rejects the format, stop. Use a
receiver UART/boot-pad flash method after verifying its pads and voltage; do
not try the image in Betaflight's STM32 flasher.

This image has not been physically validated on every Matrix 1S 5IN1 II board
revision. Treat the first installation as hardware bring-up, not a flight.

## 4. Configure Betaflight

The stock serial ELRS setup will usually already have UART3 configured. Verify
all settings rather than assuming:

1. **Ports:** enable `Serial RX` on the UART used by the onboard receiver,
   normally UART3. Do not enable Serial RX on multiple UARTs.
2. **Receiver:** select `Serial (via UART)` and provider `CRSF`.
3. **Channel map:** `AETR1234`.
4. **Modes:** assign ARM to AUX1 and ANGLE to AUX2, with their active ranges
   covering received value 2000.
5. **Failsafe:** configure and test FC-based failsafe. For a small indoor whoop,
   `Drop` is the predictable starting behavior. A blind throttle-based `Land`
   can climb or travel because the Air75 II has no altitude or position hold.
6. Save and reboot.

With props removed, open the Receiver tab. When the PC client is connected, the
expected idle values are roll/pitch/yaw 1500, throttle 1000, AUX1 1000, and
AUX2 2000. Close the PC client and verify that Betaflight reports RX loss rather
than continuing to show a healthy receiver.

Betaflight may refuse to arm while its configurator/MSP connection is active.
That is normal; channel and failsafe tests can still be performed with props
removed.

## 5. Connect the TL-WN725N

### Windows

1. Install the normal TP-Link driver if Windows did not install one.
2. Use the Windows Wi-Fi menu to connect the TL-WN725N to `Air75-Control`.
3. Run `ipconfig`. The adapter should receive an address such as
   `192.168.4.2`; the drone is `192.168.4.1`.
4. If the PC also has Ethernet or another Wi-Fi connection, Windows should
   still route `192.168.4.0/24` through the TL-WN725N. If it does not, pass the
   adapter address with `--bind-ip 192.168.4.2`.

### Linux

```bash
nmcli dev wifi connect Air75-Control password "your-password" ifname wlan1
nmcli con mod Air75-Control ipv4.never-default yes 802-11-wireless.powersave 2
nmcli con up Air75-Control
```

Use the actual interface name. `--iface wlan1` pins control traffic through
`SO_BINDTODEVICE` and may require elevated privileges.

## 6. Run the gamepad client

Install Python 3.10 or newer, then create an environment and install pygame:

```powershell
py -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python gamepad_control.py --list
.\.venv\Scripts\python gamepad_control.py
```

Default Logitech F310 controls in XInput mode:

| Input | Action |
|---|---|
| Left stick X/Y | Roll/pitch |
| Right stick X | Yaw |
| Right trigger | Climb above configured neutral throttle |
| Left trigger | Descend toward minimum throttle |
| Hold Start for 3 seconds | Request arm at minimum throttle |
| Start while armed or B | Disarm immediately |
| Back | Emergency kill and pause link |
| Escape | Stop packets and let Betaflight failsafe |

With both triggers released, throttle returns to `--hover-throttle`. Its safe
default is 1000. A value intended to approximate hover must be supplied
explicitly after calibration; without an altitude sensor it is only a fixed
throttle setpoint and cannot hold altitude.

SDL gamepad axis ordering differs on some controllers. The script exposes
`--roll-axis`, `--pitch-axis`, `--yaw-axis`, `--left-trigger-axis`, and
`--right-trigger-axis` so mappings can be corrected without editing code.

The controller prints `NO LINK`, `WAITING`, `DISARMED`, `ARMED`, or
`FAILSAFE_LATCHED`. It refuses to arm until the firmware acknowledges a
disarmed control session and throttle is at minimum.

## 7. Required props-off tests

Perform these in order:

1. Confirm the PC receives acknowledgements and displays `DISARMED`.
2. Confirm all four flight channels move in the correct direction in the
   Betaflight Receiver tab. Correct mappings before proceeding.
3. Confirm AUX1 changes only when the deliberate arm chord is used.
4. Close the PC client while its arm request is active. Confirm the channels
   immediately change to centered sticks, minimum throttle, and ARM low before
   CRSF stops and Betaflight enters RX loss/failsafe.
5. Restart the client. Confirm the receiver requires a disarmed packet and does
   not restore the previous armed state.
6. Disconnect the TL-WN725N and repeat the failsafe check.
7. Reconnect power and confirm the ESP starts unarmed and produces no RC frames
   until a valid PC session connects.
8. Only after all receiver and failsafe checks pass, test motor direction and
   arming with the props still removed.

Do the first propeller-on hover in an enclosed test area at very low height.
The onboard ESP Wi-Fi antenna was intended for receiver maintenance and its
usable control range has not been established by this project.

## 8. Video

The custom receiver firmware does not convert the onboard analog camera to
Wi-Fi. The Air75 II still transmits analog video on 5.8 GHz. `mediamtx.yml` and
the browser interface can relay a separate RTSP source or USB analog FPV
capture device, but they are independent of the gamepad control link.

## Protocol summary

PC-to-drone datagrams are 28-byte little-endian packets:

| Offset | Field |
|---:|---|
| 0 | Magic `A75W` |
| 4 | Version, currently 1 |
| 5 | Flags: bit 0 ARM, bit 1 KILL |
| 6 | Packet length, 28 |
| 8 | Random process session, uint32 |
| 12 | Monotonic sequence, uint32 |
| 16 | Roll, uint16, 1000–2000 |
| 18 | Pitch, uint16, 1000–2000 |
| 20 | Throttle, uint16, 1000–2000 |
| 22 | Yaw, uint16, 1000–2000 |
| 24 | IEEE CRC-32 over bytes 0–23 |

The ESP acknowledges accepted packets with a 20-byte `A75A` status packet.
