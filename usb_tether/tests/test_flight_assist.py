import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flight_assist import G, AssistConfig, FlightAssist, to_body, to_earth

ACC_1G = 512


class SimDrone:
    """Point-mass whoop in ANGLE mode: attitude follows the stick with a lag."""

    def __init__(self, hover_us=1320.0, drag=0.9, heading=30.0):
        self.hover_us = hover_us
        self.drag = drag
        self.heading = heading
        self.roll = self.pitch = 0.0
        self.v = [0.0, 0.0]          # north, east
        self.vz = 0.0
        self.height = 0.0
        self.a_up = 0.0
        self.kick = 0.0               # external vertical acceleration (m/s^2)

    def step(self, out, dt, angle_limit=60.0):
        target_pitch = (out.pitch_us - 1500) / 500 * angle_limit
        target_roll = (out.roll_us - 1500) / 500 * angle_limit
        self.pitch += (target_pitch - self.pitch) * min(1.0, dt / 0.08)
        self.roll += (target_roll - self.roll) * min(1.0, dt / 0.08)
        a_n, a_e = to_earth(G * math.tan(math.radians(self.pitch)),
                            G * math.tan(math.radians(self.roll)), self.heading)
        self.v[0] += (a_n - self.drag * self.v[0]) * dt
        self.v[1] += (a_e - self.drag * self.v[1]) * dt
        self.a_up = (out.throttle_us - self.hover_us) * 0.02 - 2.0 * self.vz + self.kick
        if self.height <= 0 and self.a_up < 0:
            self.a_up, self.vz = 0.0, 0.0
        self.vz += self.a_up * dt
        self.height = max(0.0, self.height + self.vz * dt)

    def telemetry(self):
        tilt = math.cos(math.radians(self.roll)) * math.cos(math.radians(self.pitch))
        az = ACC_1G * (self.a_up + G) / G / tilt
        return (self.roll, self.pitch, self.heading), (0, 0, round(az))


def fly(assist, drone, seconds, dt=0.02, **inputs):
    controls = dict(stick_right=0.0, stick_forward=0.0, climb=0.0, descend=0.0)
    controls.update(inputs)
    out = None
    for _ in range(round(seconds / dt)):
        attitude, acc = drone.telemetry()
        fly.t += dt
        out = assist.update(fly.t, attitude, acc, **controls)
        drone.step(out, dt)
        if out.disarm:
            break
    return out


fly.t = 0.0


def airborne(hover_guess=1280.0):
    assist = FlightAssist(AssistConfig(hover_guess_us=hover_guess))
    drone = SimDrone()
    for _ in range(20):
        assist.observe_rest((0, 0, ACC_1G))
    fly(assist, drone, 0.5, climb=0.6)   # take off
    fly(assist, drone, 4.0)              # settle in hover
    return assist, drone


class FlightAssistTests(unittest.TestCase):
    def test_rotation_round_trip(self):
        for heading in (0, 37, 90, 211, 359):
            f, r = to_body(*to_earth(0.3, -0.7, heading), heading)
            self.assertAlmostEqual(f, 0.3)
            self.assertAlmostEqual(r, -0.7)

    def test_ground_phase_idles_until_right_trigger(self):
        assist = FlightAssist()
        out = assist.update(0.0, (0, 0, 0), (0, 0, ACC_1G), 0.8, 0.8, 0.2, 0.0)
        self.assertEqual((out.phase, out.throttle_us, out.roll_us, out.pitch_us),
                         ("GROUND", 1000, 1500, 1500))
        out = assist.update(0.02, (0, 0, 0), (0, 0, ACC_1G), 0.0, 0.0, 0.5, 0.0)
        self.assertEqual(out.phase, "AIR")
        self.assertGreater(out.throttle_us, 1200)

    def test_brakes_to_a_stop_after_stick_release(self):
        assist, drone = airborne()
        fly(assist, drone, 2.0, stick_forward=1.0)
        speed = math.hypot(*drone.v)
        max_speed = assist.config.max_speed
        self.assertGreater(speed, 0.7 * max_speed)
        self.assertLess(speed, 1.3 * max_speed)   # model error allowance
        fly(assist, drone, 2.5)               # sticks centred
        self.assertLess(math.hypot(*drone.v), 0.08)

    def test_commanded_tilt_never_exceeds_limit(self):
        assist, drone = airborne()
        cfg = assist.config
        limit_us = cfg.max_tilt_deg / cfg.angle_limit_deg * 500 + 1
        for right, forward in ((1, 1), (-1, 0), (0, -1)):
            for _ in range(100):
                attitude, acc = drone.telemetry()
                fly.t += 0.02
                out = assist.update(fly.t, attitude, acc, right, forward, 0.0, 0.0)
                drone.step(out, 0.02)
                self.assertLessEqual(abs(out.pitch_us - 1500), limit_us)
                self.assertLessEqual(abs(out.roll_us - 1500), limit_us)

    def test_hover_trims_toward_pilot_and_holds_after_release(self):
        assist, drone = airborne(hover_guess=1280.0)   # true hover is 1320
        for _ in range(round(20 / 0.02)):              # pilot holds 0.5 m
            nudge = max(-0.6, min(0.6, 2.0 * (0.5 - drone.height) - 1.0 * drone.vz))
            fly(assist, drone, 0.02, climb=max(0.0, nudge), descend=max(0.0, -nudge))
        self.assertAlmostEqual(assist.hover_us, drone.hover_us, delta=15)
        height = drone.height
        fly(assist, drone, 3.0)                        # triggers released
        self.assertLess(abs(drone.height - height), 0.3)

    def test_bounces_are_damped(self):
        def overshoot(damping_us):
            assist, drone = airborne(hover_guess=1320.0)
            assist.config.damping_us = damping_us
            start = drone.height
            drone.kick = 15.0                          # sharp upward bump
            fly(assist, drone, 0.1)
            drone.kick = 0.0
            fly(assist, drone, 1.0)
            return drone.height - start
        self.assertLess(overshoot(150.0), 0.8 * overshoot(0.0))

    def test_triggers_request_climb_and_descent(self):
        assist, drone = airborne()
        start = drone.height
        fly(assist, drone, 1.5, climb=1.0)
        self.assertGreater(drone.height, start + 0.3)
        top = drone.height
        fly(assist, drone, 1.5, descend=0.85)     # firm, but below the land trigger
        self.assertLess(drone.height, top - 0.1)

    def test_light_trigger_and_flicks_change_throttle_gently(self):
        assist, _ = airborne(hover_guess=1320.0)
        hover = assist.hover_us
        out = assist.update(fly.t + 2.0, (0, 0, 0), None, 0, 0, 0.2, 0.0)  # settle 2 s
        self.assertLess(out.throttle_us - hover, 10)   # 20% squeeze: under 10 us
        assist2, _ = airborne(hover_guess=1320.0)
        start = assist2.throttle_us
        out = assist2.update(fly.t + 0.1, (0, 0, 0), None, 0, 0, 1.0, 0.0)  # 0.1 s flick
        self.assertLessEqual(out.throttle_us - start, 41)  # slew limit 400 us/s

    def test_full_left_trigger_lands_and_requests_disarm(self):
        assist, drone = airborne()
        out = fly(assist, drone, 1.0, descend=1.0)
        self.assertEqual(out.phase, "AIR")    # not held long enough yet
        out = fly(assist, drone, 6.0, descend=1.0)
        self.assertTrue(out.disarm)
        self.assertEqual(out.throttle_us, 1000)


if __name__ == "__main__":
    unittest.main()
