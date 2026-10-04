"""Logitech F310 control of the Air75 II over a USB tether (Betaflight MSP).

Same controls as gamepad_control.py:
  left stick X/Y   roll/pitch          right stick X   yaw
  right trigger    climb               left trigger    descend
  hold Start 3 s   arm                 Start when armed / B   disarm
  Back             kill (stop RC -> Betaflight RXLOSS failsafe)
  D-pad            trim: up/down = nose down/up, left/right = roll
  X                reset trim
  Y                switch ASSIST / MANUAL (on the ground only)

ASSIST (default): sticks request a speed, not a tilt; centred sticks brake to a
stop. Right trigger takes off, then triggers nudge height around a learned
hover throttle; holding the left trigger fully for 1.5 s lands and disarms.
See flight_assist.py.
  Escape           quit

Run configure_betaflight.py --mode msp --apply once before using this.
"""

from __future__ import annotations

import argparse
import math
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import pygame

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "wifi-2.4ghz"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from gamepad_control import (ARM_HOLD_SECONDS, DEFAULT_HOVER_THROTTLE_US, axis, button,
                             controls_are_neutral, deadzone, throttle_target, trigger)
from flight_assist import AssistConfig, FlightAssist
from msp import MspLink, describe_disable_flags, find_flight_controller


# Gentler than gamepad_control.py for flying in a small space. In ANGLE mode a
# stick offset of N us requests roughly N/500 of Betaflight's angle_limit.
DEFAULT_TILT_RANGE_US = 200
DEFAULT_YAW_RANGE_US = 200
# Flight logs (2026-10-04 04:54-04:56) show hover at about 1250-1300 once RC
# smoothing was fixed. Full trigger = 1500 leaves ~200 us of climb margin and
# puts hover near mid-trigger instead of the first quarter of travel.
DEFAULT_CLIMB_RANGE_US = 500
DEFAULT_EXPO = 0.4
# A cable hanging off the rear pulls the nose up. Pitch trim adds a constant
# nose-down request so ANGLE mode pushes the rear motors harder from takeoff.
TRIM_STEP_US = 5
TRIM_MAX_US = 150


def shape(value: float, expo: float) -> float:
    """Deadzone then expo: fine control near centre, full range at the ends."""
    value = deadzone(value)
    return (1.0 - expo) * value + expo * value ** 3


def link_fault(link: MspLink, armed_confirmed: bool, max_tilt: float = 0.0) -> str | None:
    if link.watchdog_error:
        return link.watchdog_error
    if armed_confirmed and not link.link_ok:
        return "USB LINK LOST - CONTROL STOPPED"
    # On a tether a wound-up I-term can flip the drone; past this angle ANGLE
    # mode will not recover in a small space, so disarm instead of tumbling.
    if armed_confirmed and max_tilt > 0 and len(link.attitude) >= 2:
        tilt = math.hypot(link.attitude[0], link.attitude[1])
        if tilt > max_tilt:
            return f"TILT {tilt:.0f} DEG - DISARMED"
    return None


def save_report(trace: dict, kind: str = "fault") -> Path:
    trace_dir = ROOT / "logs"
    trace_dir.mkdir(exist_ok=True)
    path = trace_dir / f"tether-{kind}-{datetime.now():%Y%m%d-%H%M%S-%f}.json"
    path.write_text(json.dumps(trace, indent=2), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", help="flight-controller COM port (auto-detected)")
    parser.add_argument("--controller", type=int, default=0)
    parser.add_argument("--roll-axis", type=int, default=0)
    parser.add_argument("--pitch-axis", type=int, default=1)
    parser.add_argument("--yaw-axis", type=int, default=2)
    parser.add_argument("--left-trigger-axis", type=int, default=4)
    parser.add_argument("--right-trigger-axis", type=int, default=5)
    parser.add_argument("--arm-button", type=int, default=7)
    parser.add_argument("--disarm-button", type=int, default=1)
    parser.add_argument("--kill-button", type=int, default=6)
    parser.add_argument("--hover-throttle", type=int, default=DEFAULT_HOVER_THROTTLE_US)
    parser.add_argument("--climb-range", type=int, default=DEFAULT_CLIMB_RANGE_US)
    parser.add_argument("--tilt-range", type=int, default=DEFAULT_TILT_RANGE_US,
                        help="roll/pitch stick range in us (gamepad_control uses 350)")
    parser.add_argument("--yaw-range", type=int, default=DEFAULT_YAW_RANGE_US)
    parser.add_argument("--expo", type=float, default=DEFAULT_EXPO, help="0 = linear, 1 = max")
    parser.add_argument("--pitch-trim", type=int, default=0,
                        help="starting pitch trim in us; positive = nose down")
    parser.add_argument("--roll-trim", type=int, default=0,
                        help="starting roll trim in us; positive = roll right")
    parser.add_argument("--trim-reset-button", type=int, default=2, help="X by default")
    parser.add_argument("--mode", choices=("assist", "manual"), default="assist")
    parser.add_argument("--mode-button", type=int, default=3, help="Y by default")
    parser.add_argument("--hover-guess", type=float, default=AssistConfig.hover_guess_us,
                        help="assist: starting hover throttle in us (it auto-trims)")
    parser.add_argument("--max-speed", type=float, default=AssistConfig.max_speed,
                        help="assist: speed at full stick, m/s")
    parser.add_argument("--assist-tilt", type=float, default=AssistConfig.max_tilt_deg,
                        help="assist: maximum tilt it will command, degrees")
    parser.add_argument("--angle-limit", type=float, default=AssistConfig.angle_limit_deg,
                        help="Betaflight angle_limit (degrees at full stick)")
    parser.add_argument("--max-tilt", type=float, default=70.0,
                        help="disarm when roll/pitch tilt exceeds this many degrees; 0 disables")
    args = parser.parse_args()
    if not 1000 <= args.hover_throttle <= 1700:
        parser.error("--hover-throttle must be between 1000 and 1700")
    if not 0 <= args.climb_range <= 1000:
        parser.error("--climb-range must be between 0 and 1000")
    if not 0 <= args.tilt_range <= 500 or not 0 <= args.yaw_range <= 500:
        parser.error("--tilt-range and --yaw-range must be between 0 and 500")
    if not 0.0 <= args.expo <= 1.0:
        parser.error("--expo must be between 0 and 1")

    port = args.port or find_flight_controller()
    if not port:
        print("No Betaflight USB port found. Plug in the tether cable.", file=sys.stderr)
        return 2

    pygame.init()
    pygame.joystick.init()
    if pygame.joystick.get_count() <= args.controller:
        print("No requested gamepad found.", file=sys.stderr)
        return 2
    pad = pygame.joystick.Joystick(args.controller)
    pad.init()
    screen = pygame.display.set_mode((840, 210))
    pygame.display.set_caption("Air75 II USB tether - Logitech F310")
    font = pygame.font.SysFont("consolas", 22)
    small_font = pygame.font.SysFont("consolas", 16)
    pygame.event.pump()
    left_trigger_rest = axis(pad, args.left_trigger_axis)
    right_trigger_rest = axis(pad, args.right_trigger_axis)

    print(f"Controller: {pad.get_name()}   Flight controller: {port}")
    print("Hold Start for 3 seconds to arm; Start or B disarms; Back kills; Esc exits.")

    link = MspLink(port).start()
    mode = args.mode
    assist = FlightAssist(AssistConfig(hover_guess_us=args.hover_guess, max_speed=args.max_speed,
                                       max_tilt_deg=args.assist_tilt,
                                       angle_limit_deg=args.angle_limit))
    throttle_us = 1000
    previous_start = previous_disarm = previous_kill = False
    trim_limit = lambda value: max(-TRIM_MAX_US, min(TRIM_MAX_US, value))
    pitch_trim = trim_limit(args.pitch_trim)
    roll_trim = trim_limit(args.roll_trim)
    arm_hold_started: float | None = None
    arm_hold_fired = False
    pending_arm_since: float | None = None
    armed_confirmed = False
    fault_message: str | None = None
    last_display = 0.0

    try:
        running = True
        while running:
            now = time.monotonic()
            for event in pygame.event.get():
                if event.type == pygame.QUIT or (event.type == pygame.KEYDOWN
                                                 and event.key == pygame.K_ESCAPE):
                    running = False
                elif event.type == pygame.JOYHATMOTION and event.value != (0, 0):
                    hat_x, hat_y = event.value
                    pitch_trim = trim_limit(pitch_trim + hat_y * TRIM_STEP_US)
                    roll_trim = trim_limit(roll_trim + hat_x * TRIM_STEP_US)
                    print(f"\nTrim: pitch {pitch_trim:+d} us, roll {roll_trim:+d} us")
                elif event.type == pygame.JOYBUTTONDOWN and event.button == args.mode_button:
                    on_ground = (assist.phase == "GROUND" if mode == "assist"
                                 else throttle_us <= 1050)
                    if on_ground:
                        mode = "manual" if mode == "assist" else "assist"
                        assist.reset()
                        print(f"\nMode: {mode.upper()}")
                    else:
                        print("\nMode change refused in the air; land first.")
                elif (event.type == pygame.JOYBUTTONDOWN
                      and event.button == args.trim_reset_button):
                    pitch_trim = roll_trim = 0
                    print("\nTrim reset")
                elif event.type == pygame.JOYDEVICEREMOVED:
                    fault_message = "GAMEPAD LOST - CONTROL STOPPED"
                    link.kill()
            if not running:
                break

            if fault_message is None:
                fault_message = link_fault(link, armed_confirmed, args.max_tilt)
                if fault_message:
                    trace = link.diagnostics()
                    trace["reason"] = fault_message
                    link.kill()
                    armed_confirmed = False
                    print(f"\n{fault_message}. Saved {save_report(trace)}")

            if fault_message is not None:
                screen.fill((18, 20, 24))
                screen.blit(font.render(fault_message, True, (235, 75, 75)), (16, 32))
                screen.blit(small_font.render("Control stopped. Close and restart while disarmed.",
                                              True, (220, 220, 220)), (16, 82))
                pygame.display.flip()
                time.sleep(0.02)
                continue

            raw_roll = axis(pad, args.roll_axis)
            raw_pitch = axis(pad, args.pitch_axis)
            raw_yaw = axis(pad, args.yaw_axis)
            left = trigger(axis(pad, args.left_trigger_axis), left_trigger_rest)
            right = trigger(axis(pad, args.right_trigger_axis), right_trigger_rest)
            roll = 1500 + roll_trim + int(shape(raw_roll, args.expo) * args.tilt_range)
            pitch = 1500 + pitch_trim - int(shape(raw_pitch, args.expo) * args.tilt_range)
            yaw = 1500 + int(shape(raw_yaw, args.expo) * args.yaw_range)

            if link.status == "ARMED" and link.link_ok:
                armed_confirmed = True
                pending_arm_since = None
                if mode == "assist":
                    out = assist.update(time.perf_counter(), link.attitude, link.acc,
                                        shape(raw_roll, args.expo), -shape(raw_pitch, args.expo),
                                        right, left)
                    roll = out.roll_us + roll_trim
                    pitch = out.pitch_us + pitch_trim
                    throttle_us = out.throttle_us
                    if out.disarm:
                        link.disarm()
                        armed_confirmed = False
                        print("\nLanded; disarmed.")
                else:
                    throttle_us = throttle_target(args.hover_throttle, left, right,
                                                  args.climb_range)
            else:
                throttle_us = 1000
                assist.reset()
                assist.observe_rest(link.acc)
                if armed_confirmed and link.status == "DISARMED":
                    # Betaflight disarmed on its own (e.g. crash or RXLOSS).
                    armed_confirmed = False
                    link.disarm()
                    print("\nBetaflight reported DISARMED.")

            link.annotation = {"mode": mode, **assist.state()}
            link.set_sticks(roll=roll, pitch=pitch, throttle=throttle_us, yaw=yaw)
            if link.paused:
                link.resume()

            disarm_pressed = button(pad, args.disarm_button)
            if disarm_pressed and not previous_disarm:
                link.disarm()
                armed_confirmed = False
                pending_arm_since = None
                print("\nDISARM requested")
            previous_disarm = disarm_pressed

            kill_pressed = button(pad, args.kill_button)
            if kill_pressed and not previous_kill:
                link.kill()
                armed_confirmed = False
                fault_message = "KILL SENT - CONTROL STOPPED"
                print("\nKILL sent; RC stopped so Betaflight enters failsafe.")
            previous_kill = kill_pressed
            if fault_message is not None:
                continue

            start_pressed = button(pad, args.arm_button)
            neutral = controls_are_neutral(raw_roll, raw_pitch, raw_yaw, left, right)
            if armed_confirmed:
                arm_hold_started = None
                arm_hold_fired = True
                if start_pressed and not previous_start:
                    link.disarm()
                    armed_confirmed = False
                    print("\nDISARM requested by Start")
            elif (start_pressed and not arm_hold_fired and link.status == "DISARMED"
                  and link.link_ok and neutral):
                if arm_hold_started is None:
                    arm_hold_started = now
                if now - arm_hold_started >= ARM_HOLD_SECONDS:
                    arm_hold_fired = True
                    if link.arm():
                        pending_arm_since = now
                        print("\nARM requested after 3-second Start hold")
                    else:
                        print(f"\nArm refused: state={link.status}")
            elif not start_pressed:
                arm_hold_started = None
                arm_hold_fired = False
            else:
                arm_hold_started = None

            if pending_arm_since is not None and now - pending_arm_since > 1.0:
                print("\nBetaflight did not arm. Blocked by: "
                      + describe_disable_flags(link.arming_disable_flags))
                link.disarm()
                pending_arm_since = None
            previous_start = start_pressed

            status = link.status if link.link_ok else "NO LINK"
            if arm_hold_started is not None:
                status = f"HOLD START {max(0.0, ARM_HOLD_SECONDS - (now - arm_hold_started)):.1f}s"
            elif pending_arm_since is not None:
                status = "ARMING..."
            blockers = ("" if link.status == "ARMED"
                        else describe_disable_flags(link.arming_disable_flags))
            if now - last_display >= 0.25:
                print(f"\r{status:18} R {roll:4} P {pitch:4} T {throttle_us:4} Y {yaw:4}",
                      end="", flush=True)
                last_display = now

            screen.fill((18, 20, 24))
            color = (75, 210, 120) if status in ("DISARMED", "ARMED") else (235, 175, 65)
            if status == "NO LINK":
                color = (235, 75, 75)
            lines = (
                (font, f"{status}    R {roll}  P {pitch}  T {throttle_us}  Y {yaw}", color),
                (small_font, "Hold Start 3s: ARM    Start/B: DISARM    Back: KILL", (220, 220, 220)),
                (small_font, f"Arming blocked by: {blockers}" if blockers else
                 (f"ASSIST {assist.phase}  hover {assist.hover_us:.0f}  speed "
                  f"{(assist.velocity[0] ** 2 + assist.velocity[1] ** 2) ** 0.5:.2f} m/s  "
                  f"(Y: manual)" if mode == "assist" else "MANUAL (ANGLE)  (Y: assist)"),
                 (160, 168, 180)),
                (small_font, f"USB tether {port}    Trim (D-pad, X resets): pitch "
                             f"{pitch_trim:+d}  roll {roll_trim:+d}",
                 (160, 168, 180)),
            )
            y = 16
            for line_font, text, line_color in lines:
                screen.blit(line_font.render(text, True, line_color), (16, y))
                y += 34
            pygame.display.flip()
            time.sleep(0.005)
    finally:
        link.stop()
        if any(sample["state"] == "ARMED" for sample in link.flight_samples):
            flight = {"args": vars(args), "flight": list(link.flight_samples)}
            print(f"\nFlight log saved to {save_report(flight, 'flight')}")
        pygame.quit()
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
