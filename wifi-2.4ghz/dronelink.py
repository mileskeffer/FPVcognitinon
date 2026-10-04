"""Binary UDP control link for the Air75 Wi-Fi-to-CRSF bridge."""

from __future__ import annotations

import errno
import secrets
import socket
import struct
import threading
import time
import zlib
from collections import deque

from windows_wifi import WifiControlSession

CONTROL_MAGIC = b"A75W"
ACK_MAGIC = b"A75A"
PROTOCOL_VERSION = 1
CONTROL_PORT = 4210
MAX_ACKS_PER_TICK = 64

FLAG_ARM = 0x01
FLAG_KILL = 0x02

CONTROL_WITHOUT_CRC = struct.Struct("<4sBBHII4H")
CONTROL_PACKET = struct.Struct("<4sBBHII4HI")
ACK_PACKET = struct.Struct("<4sBBHIII")

STATE_NAMES = {
    0: "WAITING",
    1: "DISARMED",
    2: "ARMED",
    3: "FAILSAFE_LATCHED",
}


def _clamp(value: int | float, low: int = 1000, high: int = 2000) -> int:
    return max(low, min(high, int(value)))


def build_control_packet(
    *,
    session: int,
    sequence: int,
    roll: int,
    pitch: int,
    throttle: int,
    yaw: int,
    armed: bool,
    kill: bool = False,
) -> bytes:
    flags = (FLAG_ARM if armed else 0) | (FLAG_KILL if kill else 0)
    body = CONTROL_WITHOUT_CRC.pack(
        CONTROL_MAGIC,
        PROTOCOL_VERSION,
        flags,
        CONTROL_PACKET.size,
        session & 0xFFFFFFFF,
        sequence & 0xFFFFFFFF,
        _clamp(roll),
        _clamp(pitch),
        _clamp(throttle),
        _clamp(yaw),
    )
    return body + struct.pack("<I", zlib.crc32(body) & 0xFFFFFFFF)


def parse_ack(packet: bytes) -> tuple[int, int, int] | None:
    if len(packet) != ACK_PACKET.size:
        return None
    magic, version, state, length, session, sequence, received_crc = ACK_PACKET.unpack(packet)
    if magic != ACK_MAGIC or version != PROTOCOL_VERSION or length != ACK_PACKET.size:
        return None
    if (zlib.crc32(packet[:16]) & 0xFFFFFFFF) != received_crc:
        return None
    return state, session, sequence


class DroneLink:
    """Continuously sends the latest controls and receives firmware status.

    ``bind_ip`` selects a particular local adapter on Windows. When the
    TL-WN725N is connected to Air75-Control it is normally assigned
    192.168.4.2, so use ``--bind-ip 192.168.4.2`` only if Windows routes the
    packets through another adapter.

    ``iface`` uses Linux SO_BINDTODEVICE and normally requires elevated
    privileges. It is not supported on Windows.
    """

    def __init__(
        self,
        ip: str = "192.168.4.1",
        port: int = CONTROL_PORT,
        rate_hz: int = 50,
        control_timeout_s: float = 0.25,
        bind_ip: str | None = None,
        iface: str | None = None,
    ) -> None:
        self.addr = (ip, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.bind_warning: str | None = None
        if bind_ip:
            try:
                self.sock.bind((bind_ip, 0))
            except OSError as exc:
                # Windows removes a DHCP address when its Wi-Fi interface is
                # disconnected. Keep the client alive on a wildcard socket so
                # it can recover after the Air75 access point returns.
                address_unavailable = {errno.EADDRNOTAVAIL, 10049}
                if (
                    exc.errno not in address_unavailable
                    and getattr(exc, "winerror", None) not in address_unavailable
                ):
                    self.sock.close()
                    raise
                self.sock.bind(("", 0))
                self.bind_warning = (
                    f"{bind_ip} is not assigned to this PC; using automatic routing "
                    "until the Wi-Fi adapter reconnects"
                )
        if iface:
            if not hasattr(socket, "SO_BINDTODEVICE"):
                raise RuntimeError("--iface is only supported on Linux; use --bind-ip on Windows")
            self.sock.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_BINDTODEVICE,
                iface.encode("utf-8") + b"\0",
            )
        self.sock.setblocking(False)

        self.period = 1.0 / rate_hz
        self.control_timeout_s = control_timeout_s
        self.session = secrets.randbits(32) or 1
        self.sequence = 0
        self._lock = threading.Lock()
        self._running = False
        self._paused = True
        self._thread: threading.Thread | None = None
        self._wifi = WifiControlSession()
        self._last_control_update = time.monotonic()
        self.watchdog_error: str | None = None

        self.roll = 1500
        self.pitch = 1500
        self.throttle = 1000
        self.yaw = 1500
        self.armed = False

        self.status = "NO LINK"
        self.last_reply = 0.0
        self.last_ack_sequence = 0
        self._diagnostic_lock = threading.Lock()
        self._diagnostic_events = deque(maxlen=2000)

    def _record_event(self, kind: str, **details) -> None:
        with self._diagnostic_lock:
            self._diagnostic_events.append({"time": time.monotonic(), "kind": kind, **details})

    def diagnostics(self) -> dict:
        """Copy recent PC-side timing; a successful send does not prove delivery."""
        with self._diagnostic_lock:
            events = list(self._diagnostic_events)
        gaps = {}
        for kind in ("send", "ack"):
            times = [event["time"] for event in events if event["kind"] == kind]
            gaps[f"max_{kind}_gap_ms"] = round(
                max((b - a for a, b in zip(times, times[1:])), default=0) * 1000, 1
            )
        return {"status": self.status, "link_ok": self.link_ok,
                "wifi_setup": self._wifi.message,
                "watchdog_error": self.watchdog_error,
                "session": self.session, "sequence": self.sequence,
                "last_ack_sequence": self.last_ack_sequence,
                "ack_age_ms": round((time.monotonic() - self.last_reply) * 1000, 1)
                if self.last_reply else None,
                **gaps, "events": events}

    def _send_packet(self, packet: bytes) -> None:
        details = {"sequence": struct.unpack_from("<I", packet, 12)[0], "flags": packet[5]}
        try:
            self.sock.sendto(packet, self.addr)
        except OSError as exc:
            self._record_event("send_error", error=str(exc), **details)
        else:
            self._record_event("send", **details)

    def set_sticks(
        self,
        *,
        roll: int | float | None = None,
        pitch: int | float | None = None,
        throttle: int | float | None = None,
        yaw: int | float | None = None,
    ) -> None:
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
        """Send several immediate-disarm packets before pausing."""
        with self._lock:
            self.armed = False
            self.throttle = 1000
            for _ in range(5):
                self.sequence = (self.sequence + 1) & 0xFFFFFFFF
                packet = self._packet_locked(kill=True)
                self._send_packet(packet)
        self.pause()

    def start(self) -> "DroneLink":
        if self._running:
            return self
        self._wifi.start()
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="air75-link", daemon=True)
        try:
            self._thread.start()
        except Exception:
            self._running = False
            self._thread = None
            self._wifi.close()
            raise
        return self

    def stop(self) -> None:
        # Put ARM low before closing whenever the network is still available.
        # Use ordinary disarm packets here rather than KILL: after their timeout
        # the ESP returns to WAITING and services its maintenance updater.
        if self._running:
            with self._lock:
                self.armed = False
                self.throttle = 1000
                for _ in range(5):
                    self.sequence = (self.sequence + 1) & 0xFFFFFFFF
                    packet = self._packet_locked()
                    self._send_packet(packet)
                self._paused = True
        else:
            self.pause()
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
        self.sock.close()
        self._wifi.close()

    @property
    def wifi_message(self) -> str:
        return self._wifi.message

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

    def __enter__(self) -> "DroneLink":
        self.start()
        self.resume()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def _packet_locked(self, *, kill: bool = False) -> bytes:
        return build_control_packet(
            session=self.session,
            sequence=self.sequence,
            roll=self.roll,
            pitch=self.pitch,
            throttle=self.throttle,
            yaw=self.yaw,
            armed=self.armed,
            kill=kill,
        )

    def _send_current(self) -> None:
        with self._lock:
            if self._paused:
                return
            control_age = time.monotonic() - self._last_control_update
            if control_age > self.control_timeout_s:
                self.watchdog_error = f"CONTROL LOOP STALLED ({control_age * 1000:.0f} ms)"
                # The UI/gamepad loop is stale even though this sender thread is
                # alive. Send explicit disarm packets before pausing. Because
                # the lock is held, build and transmit them directly here.
                self.armed = False
                self.throttle = 1000
                for _ in range(5):
                    self.sequence = (self.sequence + 1) & 0xFFFFFFFF
                    packet = self._packet_locked(kill=True)
                    self._send_packet(packet)
                self._paused = True
                return
            self.sequence = (self.sequence + 1) & 0xFFFFFFFF
            packet = self._packet_locked()
        self._send_packet(packet)

    def _receive_acks(self) -> None:
        # A busy receive queue must not starve control sends or the watchdog.
        for _ in range(MAX_ACKS_PER_TICK):
            try:
                packet, source = self.sock.recvfrom(64)
            except (BlockingIOError, OSError):
                return
            if source[0] != self.addr[0]:
                continue
            parsed = parse_ack(packet)
            if parsed is None:
                continue
            state, session, sequence = parsed
            if session != self.session:
                continue
            self.status = STATE_NAMES.get(state, f"UNKNOWN({state})")
            self.last_ack_sequence = sequence
            self.last_reply = time.monotonic()
            self._record_event("ack", sequence=sequence, state=self.status)

    def _loop(self) -> None:
        next_send = time.monotonic()
        while self._running:
            self._send_current()
            self._receive_acks()
            next_send += self.period
            delay = next_send - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_send = time.monotonic()
