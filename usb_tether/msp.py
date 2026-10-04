"""MSP v1 over the flight controller's USB port: RC input and status.

Betaflight must use the MSP receiver (``configure_betaflight.py --mode msp``).
Each MSP_SET_RAW_RC frame refreshes Betaflight's receiver; if frames stop
(cable pulled, PC process killed) Betaflight's own RXLOSS failsafe takes over.
"""

from __future__ import annotations

import struct
import threading
import time
from collections import deque

import serial
import serial.tools.list_ports

MSP_API_VERSION = 1
MSP_FEATURE_CONFIG = 36
MSP_SET_FEATURE_CONFIG = 37
MSP_SET_REBOOT = 68
MSP_STATUS = 101
MSP_RAW_IMU = 102
MSP_MOTOR = 104
MSP_RC = 105
MSP_ATTITUDE = 108
MSP_DEBUG = 254
MSP_SET_RAW_RC = 200
MSP_EEPROM_WRITE = 250

FEATURE_RX_SERIAL = 1 << 3
FEATURE_RX_MSP = 1 << 14

# Channel order after Betaflight's default AETR map.
ROLL, PITCH, THROTTLE, YAW, AUX1, AUX2, AUX3, AUX4 = range(8)
CHANNEL_COUNT = 8

# Air75 II aux modes (backups/air75ii_betaflight_diff_all_2026-10-03.txt):
# ARM = AUX1 1700-2100, ANGLE = AUX2 900-1300, HORIZON = AUX2 1300-1700,
# turtle = AUX3 1700-2100, beeper = AUX4 1700-2100.
ARM_ON_US = 2000
ARM_OFF_US = 1000
ANGLE_US = 1000
AUX_OFF_US = 1000

# Betaflight 4.5 armingDisableFlags_e bit order. Newer builds may append or
# insert flags, so the raw mask is always shown alongside these names.
ARMING_DISABLE_NAMES = (
    "NOGYRO", "FAILSAFE", "RXLOSS", "BADRX", "BOXFAILSAFE", "RUNAWAY", "CRASH",
    "THROTTLE", "ANGLE", "BOOTGRACE", "NOPREARM", "LOAD", "CALIB", "CLI", "CMS",
    "BST", "MSP", "PARALYZE", "GPS", "RESC", "RPMFILTER", "REBOOT_REQD",
    "DSHOT_BBANG", "ACC_CALIB", "MOTOR_PROTO", "ARMSWITCH",
)

STM32_VCP = (0x0483, 0x5740)


def _clamp(value: int | float, low: int = 1000, high: int = 2000) -> int:
    return max(low, min(high, int(value)))


def encode_request(command: int, payload: bytes = b"") -> bytes:
    if len(payload) > 255:
        raise ValueError("MSP v1 payload too long")
    checksum = len(payload) ^ command
    for value in payload:
        checksum ^= value
    return b"$M<" + bytes((len(payload), command)) + payload + bytes((checksum,))


class FrameParser:
    """Incrementally extract (command, payload) from MSP v1 responses."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.errors = 0

    def feed(self, data: bytes) -> list[tuple[int, bytes, bool]]:
        """Return (command, payload, ok) frames; ok is False for '$M!' errors."""
        self._buffer.extend(data)
        frames = []
        while True:
            start = -1
            for marker in (b"$M>", b"$M!"):
                index = self._buffer.find(marker)
                if index >= 0 and (start < 0 or index < start):
                    start = index
            if start < 0:
                # Keep a possible partial header.
                del self._buffer[:-2]
                return frames
            del self._buffer[:start]
            if len(self._buffer) < 6:
                return frames
            length = self._buffer[3]
            if len(self._buffer) < 6 + length:
                return frames
            command = self._buffer[4]
            payload = bytes(self._buffer[5:5 + length])
            checksum = length ^ command
            for value in payload:
                checksum ^= value
            ok = self._buffer[2:3] == b">"
            if checksum == self._buffer[5 + length]:
                frames.append((command, payload, ok))
            else:
                self.errors += 1
            del self._buffer[:6 + length]


def rc_payload(channels: list[int]) -> bytes:
    return struct.pack(f"<{len(channels)}H", *(_clamp(value) for value in channels))


def parse_status(payload: bytes) -> tuple[bool, int]:
    """Return (armed, arming_disable_flags) from an MSP_STATUS payload."""
    if len(payload) < 11:
        raise ValueError("MSP_STATUS payload too short")
    flight_mode_flags = struct.unpack_from("<I", payload, 6)[0]
    armed = bool(flight_mode_flags & 1)  # BOXARM is always the first active box
    disable_flags = 0
    # cycle(2) i2c(2) sensors(2) modes(4) profile(1) load(2) gyro_cycle(2)
    offset = 15
    if len(payload) > offset:
        offset += 1 + payload[offset]  # extra flight-mode flag bytes
        if len(payload) >= offset + 5:
            disable_flags = struct.unpack_from("<I", payload, offset + 1)[0]
    return armed, disable_flags


def describe_disable_flags(flags: int) -> str:
    names = [name for bit, name in enumerate(ARMING_DISABLE_NAMES) if flags >> bit & 1]
    unknown = flags & ~((1 << len(ARMING_DISABLE_NAMES)) - 1)
    if unknown:
        names.append(f"0x{unknown:x}")
    return " ".join(names) or "none"


def find_flight_controller() -> str | None:
    for port in serial.tools.list_ports.comports():
        if (port.vid, port.pid) == STM32_VCP:
            return port.device
    return None


class MspLink:
    """Drop-in replacement for ``DroneLink`` that sends RC over USB MSP.

    A background thread sends MSP_SET_RAW_RC at ``rate_hz`` and polls
    MSP_STATUS. ``status`` is "ARMED", "DISARMED" or "NO LINK".
    """

    def __init__(self, port: str, rate_hz: int = 50, status_hz: int = 20,
                 control_timeout_s: float = 0.25) -> None:
        self.port_name = port
        self.serial = serial.Serial(port, 115200, timeout=0, write_timeout=0.1)
        self.period = 1.0 / rate_hz
        self.status_period = 1.0 / status_hz
        self.control_timeout_s = control_timeout_s
        self._parser = FrameParser()
        self._lock = threading.Lock()
        self._running = False
        self._paused = True
        self._thread: threading.Thread | None = None
        self._last_control_update = time.monotonic()
        self.watchdog_error: str | None = None
        self.bind_warning: str | None = None
        self.wifi_message = ""

        self.roll = 1500
        self.pitch = 1500
        self.throttle = 1000
        self.yaw = 1500
        self.armed = False

        self.status = "NO LINK"
        self.arming_disable_flags = 0
        self.last_reply = 0.0
        self.sequence = 0
        self.rc_acks = 0
        self.fc_channels: tuple[int, ...] = ()  # what Betaflight reports receiving
        self.attitude: tuple[float, float, int] = ()  # roll deg, pitch deg, heading deg
        self.motors: tuple[int, ...] = ()
        self.debug: tuple[int, ...] = ()  # Betaflight debug[] for the active debug_mode
        self.acc: tuple[int, ...] = ()  # raw accelerometer x, y, z (MSP_RAW_IMU units)
        self.annotation: dict = {}  # app state (e.g. flight assist) copied into samples
        self._diagnostic_events = deque(maxlen=2000)
        # Flight recorder: one sample per status poll (20 Hz -> 30 minutes).
        self.flight_samples = deque(maxlen=36000)

    def _record_event(self, kind: str, **details) -> None:
        self._diagnostic_events.append({"time": time.perf_counter(), "kind": kind, **details})

    def diagnostics(self) -> dict:
        events = list(self._diagnostic_events)
        times = [event["time"] for event in events if event["kind"] == "status"]
        return {"transport": "usb-msp", "port": self.port_name, "status": self.status,
                "link_ok": self.link_ok, "watchdog_error": self.watchdog_error,
                "arming_disable_flags": describe_disable_flags(self.arming_disable_flags),
                "sequence": self.sequence, "rc_acks": self.rc_acks,
                "parse_errors": self._parser.errors,
                "max_status_gap_ms": round(max((b - a for a, b in zip(times, times[1:])),
                                               default=0) * 1000, 1),
                "events": events, "flight": list(self.flight_samples)}

    def _record_sample_locked(self) -> None:
        self.flight_samples.append({
            "t": round(time.perf_counter(), 3),
            "state": self.status,
            "sent": [self.roll, self.pitch, self.throttle if self.armed else 1000, self.yaw,
                     int(self.armed)],
            "att": list(self.attitude),
            "motors": list(self.motors[:4]),
            # Betaflight's received channels (R, P, Y, T) and debug values.
            "rc": list(self.fc_channels[:4]),
            "flags": self.arming_disable_flags,
            "debug": list(self.debug),
            "acc": list(self.acc),
            "assist": dict(self.annotation),
        })

    # --- control API shared with DroneLink -------------------------------

    def set_sticks(self, *, roll=None, pitch=None, throttle=None, yaw=None) -> None:
        with self._lock:
            self._last_control_update = time.monotonic()
            if roll is not None:
                self.roll = _clamp(roll)
            if pitch is not None:
                self.pitch = _clamp(pitch)
            if throttle is not None:
                self.throttle = _clamp(throttle)
            if yaw is not None:
                self.yaw = _clamp(yaw)

    def arm(self) -> bool:
        with self._lock:
            if self.throttle > 1050 or self.status != "DISARMED":
                return False
            self.armed = True
            self._last_control_update = time.monotonic()
            return True

    def disarm(self) -> None:
        with self._lock:
            self.armed = False
            self.throttle = 1000
            self._last_control_update = time.monotonic()

    def kill(self) -> None:
        """Send disarm frames, then stop RC so Betaflight enters RXLOSS failsafe."""
        with self._lock:
            self.armed = False
            self.throttle = 1000
            for _ in range(5):
                self._write_rc_locked()
            self._paused = True

    def pause(self) -> None:
        with self._lock:
            self._paused = True
            self.armed = False

    def resume(self) -> None:
        with self._lock:
            self._last_control_update = time.monotonic()
            self.watchdog_error = None
            self._paused = False

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    @property
    def link_ok(self) -> bool:
        return (time.monotonic() - self.last_reply) < 0.5

    def start(self) -> "MspLink":
        if self._running:
            return self
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="air75-msp", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._running:
            with self._lock:
                self.armed = False
                self.throttle = 1000
                for _ in range(5):
                    self._write_rc_locked()
                self._paused = True
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
        self.serial.close()

    def __enter__(self) -> "MspLink":
        self.start()
        self.resume()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    # --- transport -------------------------------------------------------

    def channels_locked(self) -> list[int]:
        channels = [1500] * CHANNEL_COUNT
        channels[ROLL] = self.roll
        channels[PITCH] = self.pitch
        channels[THROTTLE] = self.throttle if self.armed else 1000
        channels[YAW] = self.yaw
        channels[AUX1] = ARM_ON_US if self.armed else ARM_OFF_US
        channels[AUX2] = ANGLE_US
        channels[AUX3] = AUX_OFF_US
        channels[AUX4] = AUX_OFF_US
        return channels

    def _write(self, frame: bytes, kind: str) -> None:
        try:
            self.serial.write(frame)
        except (serial.SerialException, OSError) as exc:
            self._record_event("write_error", error=str(exc))
            self.status = "NO LINK"
        else:
            if not kind.endswith("_request"):
                self._record_event(kind)

    def _write_rc_locked(self) -> None:
        self.sequence += 1
        self._write(encode_request(MSP_SET_RAW_RC, rc_payload(self.channels_locked())), "send")

    def _send_current(self) -> None:
        with self._lock:
            if self._paused:
                return
            control_age = time.monotonic() - self._last_control_update
            if control_age > self.control_timeout_s:
                self.watchdog_error = f"CONTROL LOOP STALLED ({control_age * 1000:.0f} ms)"
                self.armed = False
                self.throttle = 1000
                for _ in range(5):
                    self._write_rc_locked()
                self._paused = True
                return
            self._write_rc_locked()

    def _read_replies(self) -> None:
        try:
            data = self.serial.read(self.serial.in_waiting or 0)
        except (serial.SerialException, OSError) as exc:
            self._record_event("read_error", error=str(exc))
            self.status = "NO LINK"
            return
        for command, payload, ok in self._parser.feed(data):
            if command == MSP_SET_RAW_RC:
                if ok:
                    self.rc_acks += 1
                else:
                    self._record_event("rc_rejected")
            elif command == MSP_STATUS and ok:
                try:
                    armed, flags = parse_status(payload)
                except ValueError:
                    continue
                self.arming_disable_flags = flags
                self.status = "ARMED" if armed else "DISARMED"
                self.last_reply = time.monotonic()
                self._record_event("status", state=self.status, flags=flags)
            elif command == MSP_RC and ok:
                self.fc_channels = struct.unpack(f"<{len(payload) // 2}H", payload)
            elif command == MSP_ATTITUDE and ok and len(payload) >= 6:
                roll, pitch, heading = struct.unpack_from("<hhh", payload)
                self.attitude = (roll / 10, pitch / 10, heading)
            elif command == MSP_RAW_IMU and ok and len(payload) >= 6:
                self.acc = struct.unpack_from("<hhh", payload)
            elif command == MSP_DEBUG and ok:
                self.debug = struct.unpack(f"<{len(payload) // 2}h", payload)
            elif command == MSP_MOTOR and ok:
                self.motors = struct.unpack(f"<{len(payload) // 2}H", payload)

    def _loop(self) -> None:
        # time.monotonic() ticks every 15.6 ms on Windows, which made frames go
        # out 0-63 ms apart; Betaflight's auto RC smoothing then never locked on
        # to a frame rate. perf_counter() is sub-millisecond.
        next_send = time.perf_counter()
        next_status = next_send
        while self._running:
            now = time.perf_counter()
            self._send_current()
            # Attitude and accelerometer every frame (50 Hz) for the flight assist.
            self._write(encode_request(MSP_ATTITUDE), "attitude_request")
            self._write(encode_request(MSP_RAW_IMU), "imu_request")
            if now >= next_status:
                self._write(encode_request(MSP_STATUS), "status_request")
                self._write(encode_request(MSP_RC), "rc_request")
                self._write(encode_request(MSP_MOTOR), "motor_request")
                self._write(encode_request(MSP_DEBUG), "debug_request")
                with self._lock:
                    self._record_sample_locked()
                next_status = now + self.status_period
            self._read_replies()
            next_send += self.period
            delay = next_send - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            else:
                next_send = time.perf_counter()
