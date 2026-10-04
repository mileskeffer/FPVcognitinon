"""Flight assist: stick input requests a velocity; the assist picks tilt and throttle.

The Air75 II has only a gyro and accelerometer (no barometer, optical flow or
GPS), so velocity is estimated, not measured:

* Horizontal: a drone tilted by angle a accelerates at about g*tan(a), minus air
  drag. Integrating that from Betaflight's attitude gives the drone's own
  velocity. Centred sticks request zero velocity, so after every move the
  assist tilts against the estimated momentum and brakes to a stop instead of
  coasting. It cannot see external pushes (a taut cable, air from walls), so
  it holds still relative to those forces, not at a fixed spot.
* Vertical: an accelerometer cannot see a steady climb (it reads 1 g at any
  constant speed), so there is no true altitude hold. Released triggers give
  the hover throttle; the triggers nudge it up/down; the vertical
  accelerometer damps bounces. The hover throttle auto-trims: if the pilot
  keeps nudging one way to hold height, the neutral slowly follows, so the
  next release holds there. Expect slow drift that needs occasional nudges.

Phases: GROUND (idle throttle until the right trigger is pulled), AIR, and
LANDING (left trigger held fully for ``land_hold_s``: throttle ramps down, then
the assist asks for disarm).

Betaflight conventions used here (checked against flight logs): positive pitch
attitude and pitch stick > 1500 mean nose down (forward); positive roll and
roll stick > 1500 mean right side down. In ANGLE mode a stick offset of N us
requests N/500 of ``angle_limit``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

G = 9.81


@dataclass
class AssistConfig:
    max_speed: float = 0.5          # m/s at full stick
    max_accel: float = 1.5          # m/s^2: limits target changes and commanded tilt
    velocity_gain: float = 2.5      # 1/s: accel commanded per m/s of velocity error
    drag: float = 0.8               # 1/s: air drag in the velocity model
    max_tilt_deg: float = 12.0
    angle_limit_deg: float = 60.0   # Betaflight angle_limit
    trigger_us: float = 120.0       # throttle offset from hover at full trigger
    trigger_expo: float = 0.8       # 0 linear; 0.8: half trigger gives ~20% of full
    throttle_slew_us: float = 400.0  # max throttle change per second (no jumps)
    # In flight, MSP_RAW_IMU z swings +/-35% from motor vibration (05:53-06:10
    # logs), so accelerometer climb-rate damping mostly injects noise. Off.
    damping_us: float = 0.0         # us of throttle per m/s of estimated climb rate
    hover_trim_s: float = 15.0      # time constant of hover auto-trim
    trim_max_us: float = 40.0       # only gentle nudges trim; bigger ones are moves
    hover_guess_us: float = 1280.0  # tether logs 2026-10-04: hover at ~1250-1300
    hover_min_us: float = 1150.0
    hover_max_us: float = 1500.0
    vz_leak: float = 1.0            # 1/s: climb-rate estimate forgets (bias, steady climb)
    takeoff_trigger: float = 0.3
    land_trigger: float = 0.9
    land_hold_s: float = 1.5
    land_rate_us: float = 150.0     # us/s throttle ramp while landing
    land_done_us: float = 1080.0


@dataclass
class AssistOutput:
    roll_us: int
    pitch_us: int
    throttle_us: int
    phase: str
    disarm: bool = False


def to_earth(forward: float, right: float, heading_deg: float) -> tuple[float, float]:
    h = math.radians(heading_deg)
    return (forward * math.cos(h) - right * math.sin(h),
            forward * math.sin(h) + right * math.cos(h))


def to_body(north: float, east: float, heading_deg: float) -> tuple[float, float]:
    h = math.radians(heading_deg)
    return (north * math.cos(h) + east * math.sin(h),
            -north * math.sin(h) + east * math.cos(h))


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class FlightAssist:
    def __init__(self, config: AssistConfig | None = None) -> None:
        self.config = config or AssistConfig()
        self.hover_us = self.config.hover_guess_us
        self.acc_1g: float | None = None  # raw accelerometer z reading at rest
        self.reset()

    def reset(self) -> None:
        """Back to GROUND; keeps the learned hover throttle and accelerometer scale."""
        self.phase = "GROUND"
        self.velocity = [0.0, 0.0]         # estimated north, east (m/s)
        self.target = [0.0, 0.0]           # rate-limited requested velocity
        self.climb_rate = 0.0              # estimated m/s, up positive
        self.throttle_us = 1000.0
        self.land_held_since: float | None = None
        self.last_time: float | None = None

    def observe_rest(self, acc: tuple[int, ...] | None) -> None:
        """Track the accelerometer's 1 g reading while disarmed and still."""
        if acc and len(acc) >= 3 and acc[2]:
            z = float(acc[2])
            self.acc_1g = z if self.acc_1g is None else 0.95 * self.acc_1g + 0.05 * z

    def state(self) -> dict:
        return {"phase": self.phase, "v": [round(v, 3) for v in self.velocity],
                "target": [round(v, 3) for v in self.target],
                "vz": round(self.climb_rate, 3), "hover": round(self.hover_us, 1),
                "throttle": round(self.throttle_us)}

    def update(self, now: float, attitude: tuple[float, ...], acc: tuple[int, ...] | None,
               stick_right: float, stick_forward: float,
               climb: float, descend: float) -> AssistOutput:
        """Sticks in -1..1 (already deadzoned/shaped), triggers in 0..1."""
        cfg = self.config
        dt = 0.0 if self.last_time is None else _clamp(now - self.last_time, 0.0, 0.1)
        self.last_time = now
        roll_deg, pitch_deg, heading = (attitude + (0.0, 0.0, 0.0))[:3] if attitude else (0, 0, 0)

        if self.phase == "GROUND":
            self.observe_rest(acc)
            if climb > cfg.takeoff_trigger:
                self.phase = "AIR"
                self.velocity = [0.0, 0.0]
                self.target = [0.0, 0.0]
                self.climb_rate = 0.0
                self.throttle_us = self.hover_us
            else:
                self.throttle_us = 1000.0
                return AssistOutput(1500, 1500, 1000, self.phase)

        # --- horizontal: velocity estimate and braking -----------------------
        a_forward = G * math.tan(math.radians(_clamp(pitch_deg, -80, 80)))
        a_right = G * math.tan(math.radians(_clamp(roll_deg, -80, 80)))
        a_north, a_east = to_earth(a_forward, a_right, heading)
        self.velocity[0] += (a_north - cfg.drag * self.velocity[0]) * dt
        self.velocity[1] += (a_east - cfg.drag * self.velocity[1]) * dt

        landing = self.phase == "LANDING"
        want = (0.0, 0.0) if landing else to_earth(
            _clamp(stick_forward, -1, 1) * cfg.max_speed,
            _clamp(stick_right, -1, 1) * cfg.max_speed, heading)
        delta = (want[0] - self.target[0], want[1] - self.target[1])
        distance = math.hypot(*delta)
        step = cfg.max_accel * dt
        if distance > step > 0:
            delta = (delta[0] * step / distance, delta[1] * step / distance)
        self.target = [self.target[0] + delta[0], self.target[1] + delta[1]]

        command = [cfg.velocity_gain * (t - v) + cfg.drag * t
                   for t, v in zip(self.target, self.velocity)]
        magnitude = math.hypot(*command)
        if magnitude > cfg.max_accel:
            command = [c * cfg.max_accel / magnitude for c in command]
        c_forward, c_right = to_body(command[0], command[1], heading)
        tilt_forward = _clamp(math.degrees(math.atan(c_forward / G)),
                              -cfg.max_tilt_deg, cfg.max_tilt_deg)
        tilt_right = _clamp(math.degrees(math.atan(c_right / G)),
                            -cfg.max_tilt_deg, cfg.max_tilt_deg)
        pitch_us = round(1500 + tilt_forward / cfg.angle_limit_deg * 500)
        roll_us = round(1500 + tilt_right / cfg.angle_limit_deg * 500)

        # --- vertical: climb-rate estimate and hover throttle -----------------
        if acc and len(acc) >= 3 and self.acc_1g:
            tilt_factor = (math.cos(math.radians(roll_deg)) * math.cos(math.radians(pitch_deg)))
            a_up = acc[2] / self.acc_1g * G * tilt_factor - G
            self.climb_rate += (a_up - cfg.vz_leak * self.climb_rate) * dt

        if landing:
            if climb > cfg.takeoff_trigger:
                self.phase = "AIR"  # pilot aborted the landing
            else:
                self.throttle_us = max(1000.0, self.throttle_us - cfg.land_rate_us * dt)
                if self.throttle_us <= cfg.land_done_us:
                    self.phase = "GROUND"
                    self.throttle_us = 1000.0
                    return AssistOutput(1500, 1500, 1000, "LANDED", disarm=True)
                return AssistOutput(roll_us, pitch_us, round(self.throttle_us), self.phase)

        nudge = _clamp(climb, 0, 1) - _clamp(descend, 0, 1)
        offset = cfg.trigger_us * ((1 - cfg.trigger_expo) * nudge + cfg.trigger_expo * nudge ** 3)
        if abs(offset) <= cfg.trim_max_us:
            self.hover_us = _clamp(self.hover_us + offset * dt / cfg.hover_trim_s,
                                   cfg.hover_min_us, cfg.hover_max_us)
        throttle = _clamp(self.hover_us + offset - cfg.damping_us * self.climb_rate, 1000, 2000)
        step = cfg.throttle_slew_us * dt
        self.throttle_us = _clamp(throttle, self.throttle_us - step, self.throttle_us + step)

        if descend >= cfg.land_trigger:
            if self.land_held_since is None:
                self.land_held_since = now
            elif now - self.land_held_since >= cfg.land_hold_s:
                self.phase = "LANDING"
        else:
            self.land_held_since = None
        return AssistOutput(roll_us, pitch_us, round(self.throttle_us), self.phase)
