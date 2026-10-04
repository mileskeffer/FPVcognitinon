"""Direct gamepad controller for the Air75 Wi-Fi bridge.

Logitech F310 mapping (X-input mode):
  left stick X/Y   roll/pitch
  right stick X    yaw
  right trigger    climb above the configured neutral throttle
  left trigger     descend below the configured neutral throttle
  hold Start 3 s   arm from minimum throttle
  Start when armed disarm immediately
  B                backup immediate disarm
  Back             emergency kill and pause the radio link
  Escape           quit and let Betaflight enter configured failsafe
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import Future
from datetime import datetime
from pathlib import Path

import pygame

from dronelink import DroneLink
from receiver_diagnostics import fetch_diagnostics

ROLL_PITCH_RANGE = 350
YAW_RANGE = 300
DEFAULT_HOVER_THROTTLE_US = 1000
DEFAULT_CLIMB_RANGE_US = 500
ARM_HOLD_SECONDS = 3.0
DEADZONE = 0.08


def deadzone(value: float) -> float:
    if abs(value) <= DEADZONE:
        return 0.0
    magnitude = (abs(value) - DEADZONE) / (1.0 - DEADZONE)
    return magnitude if value > 0 else -magnitude


def trigger(value: float, rest: float) -> float:
    """Normalize triggers that rest at either -1 (common) or 0."""
    if rest < -0.5:
        return max(0.0, min(1.0, (value - rest) / (1.0 - rest)))
    return max(0.0, min(1.0, value))


def throttle_target(hover_throttle: int, left: float, right: float, climb_range: int) -> int:
    """Return trigger-controlled throttle around the configured neutral point."""
    descent_range = hover_throttle - 1000
    target = hover_throttle + (right * climb_range) - (left * descent_range)
    return max(1000, min(2000, int(round(target))))


def controls_are_neutral(roll_input: float, pitch_input: float, yaw_input: float,
                         left_trigger: float, right_trigger: float) -> bool:
    return (
        abs(roll_input) <= DEADZONE
        and abs(pitch_input) <= DEADZONE
        and abs(yaw_input) <= DEADZONE
        and left_trigger <= 0.05
        and right_trigger <= 0.05
    )


def control_link_fault(link: DroneLink, armed_confirmed: bool) -> str | None:
    if link.watchdog_error:
        return link.watchdog_error
    if link.link_ok and link.status == "FAILSAFE_LATCHED":
        return "RECEIVER FAILSAFE - CONTROL STOPPED"
    if armed_confirmed and not link.link_ok:
        return "ACKNOWLEDGEMENTS LOST - CONTROL STOPPED"
    return None


def axis(joystick: pygame.joystick.JoystickType, index: int) -> float:
    return joystick.get_axis(index) if index < joystick.get_numaxes() else 0.0


def button(joystick: pygame.joystick.JoystickType, index: int) -> bool:
    return bool(joystick.get_button(index)) if index < joystick.get_numbuttons() else False


def read_receiver_diagnostics(ip: str, timeout: float = 2.0) -> dict:
    try:
        return fetch_diagnostics(ip, timeout=timeout)
    except (OSError, ValueError) as exc:
        return {"error": str(exc)}


def background_job(function, *args) -> Future:
    """Keep diagnostic socket/disk waits off the pygame event loop."""
    result = Future()

    def run():
        try:
            result.set_result(function(*args))
        except Exception as exc:
            result.set_exception(exc)

    # A stuck diagnostic request must not prevent closing the controller.
    threading.Thread(target=run, name="air75-diagnostics", daemon=True).start()
    return result


def save_fault_report(ip: str, trace: dict) -> str:
    # Control is already stopped. The receiver responds only after its disarm
    # burst; never poll diagnostics in flight.
    trace["receiver_after"] = read_receiver_diagnostics(ip)
    trace_dir = Path(__file__).resolve().parent / "logs"
    trace_dir.mkdir(exist_ok=True)
    trace_path = trace_dir / f"gamepad-fault-{datetime.now():%Y%m%d-%H%M%S-%f}.json"
    trace_path.write_text(json.dumps(trace, indent=2), encoding="utf-8")
    return str(trace_path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Control Air75 II through the ESP8285 Wi-Fi bridge")
    parser.add_argument("--drone-ip", default="192.168.4.1")
    parser.add_argument("--bind-ip", help="local TL-WN725N IPv4 address; useful on Windows")
    parser.add_argument("--iface", help="Linux interface such as wlan1; may require root")
    parser.add_argument("--controller", type=int, default=0)
    parser.add_argument("--roll-axis", type=int, default=0, help="F310 left-stick X")
    parser.add_argument("--pitch-axis", type=int, default=1, help="F310 left-stick Y")
    parser.add_argument("--yaw-axis", type=int, default=2, help="F310 right-stick X")
    parser.add_argument("--left-trigger-axis", type=int, default=4, help="F310 left trigger")
    parser.add_argument("--right-trigger-axis", type=int, default=5, help="F310 right trigger")
    parser.add_argument("--arm-button", type=int, default=7, help="Start by default")
    parser.add_argument("--disarm-button", type=int, default=1, help="B by default")
    parser.add_argument("--kill-button", type=int, default=6, help="Back by default")
    parser.add_argument(
        "--hover-throttle",
        type=int,
        default=DEFAULT_HOVER_THROTTLE_US,
        help="neutral trigger throttle; 1000 is the safe motor-idle default",
    )
    parser.add_argument(
        "--climb-range",
        type=int,
        default=DEFAULT_CLIMB_RANGE_US,
        help="maximum throttle added by the right trigger",
    )
    parser.add_argument("--list", action="store_true", help="list controllers and exit")
    args = parser.parse_args()
    if not 1000 <= args.hover_throttle <= 1700:
        parser.error("--hover-throttle must be between 1000 and 1700")
    if not 0 <= args.climb_range <= 1000:
        parser.error("--climb-range must be between 0 and 1000")

    pygame.init()
    pygame.joystick.init()

    if args.list:
        for index in range(pygame.joystick.get_count()):
            pad = pygame.joystick.Joystick(index)
            print(f"{index}: {pad.get_name()} ({pad.get_numaxes()} axes, {pad.get_numbuttons()} buttons)")
        return 0

    if pygame.joystick.get_count() <= args.controller:
        print("No requested gamepad found. Run with --list after connecting it.", file=sys.stderr)
        return 2

    pad = pygame.joystick.Joystick(args.controller)
    pad.init()
    screen = pygame.display.set_mode((840, 210))
    pygame.display.set_caption("Air75 II Wi-Fi control - Logitech F310")
    font = pygame.font.SysFont("consolas", 22)
    small_font = pygame.font.SysFont("consolas", 16)
    pygame.event.pump()
    left_trigger_rest = axis(pad, args.left_trigger_axis)
    right_trigger_rest = axis(pad, args.right_trigger_axis)

    print(f"Controller: {pad.get_name()}")
    print("Connect the TL-WN725N to Air75-Control before attempting to arm.")
    print("Hold Start for 3 seconds to arm; Start or B disarms; Back kills; Esc exits.")
    print(f"Neutral trigger throttle: {args.hover_throttle} us")
    if args.hover_throttle == 1000:
        print("Automatic hover throttle is disabled; use --hover-throttle only after calibration.")

    link = DroneLink(ip=args.drone_ip, bind_ip=args.bind_ip, iface=args.iface)
    if link.bind_warning:
        print(f"WARNING: {link.bind_warning}")
    receiver_baseline = background_job(read_receiver_diagnostics, args.drone_ip, 0.7)
    link.start()
    if link.wifi_message:
        print(link.wifi_message)
    throttle_us = 1000
    previous_start = False
    previous_disarm = False
    previous_kill = False
    arm_hold_started: float | None = None
    arm_hold_fired = False
    pending_arm_since: float | None = None
    armed_confirmed = False
    fault_message: str | None = None
    fault_report: Future | None = None
    last_display = 0.0

    try:
        running = True
        while running:
            now = time.monotonic()
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                    running = False
                elif event.type == pygame.JOYDEVICEREMOVED:
                    fault_message = "GAMEPAD LOST - CONTROL STOPPED"
                    print("Gamepad disconnected; KILL sent and control stopped.")
                    link.kill()

            if not running:
                break

            if fault_message is None and not receiver_baseline.done():
                # Stay disarmed and silent until the read-only baseline is
                # captured, but continue handling Escape/window-close events.
                screen.fill((18, 20, 24))
                screen.blit(font.render("Checking receiver... Esc to exit", True,
                                        (220, 220, 220)), (16, 32))
                pygame.display.flip()
                time.sleep(0.02)
                continue

            if fault_message is None:
                fault_message = control_link_fault(link, armed_confirmed)
                if fault_message:
                    # Capture before our response KILL so it cannot be mistaken
                    # for the cause of the receiver's original failsafe.
                    fault_trace = link.diagnostics()
                    fault_trace["reason"] = fault_message
                    link.kill()
                    armed_confirmed = False
                    pending_arm_since = None
                    print(f"\n{fault_message}. Disarm sent; restart required.")
                    fault_trace["receiver_before"] = receiver_baseline.result()
                    fault_report = background_job(save_fault_report, args.drone_ip, fault_trace)

            if fault_message is not None:
                if fault_report is not None and fault_report.done():
                    try:
                        print(f"Fault timing saved to {fault_report.result()}")
                    except Exception as exc:
                        print(f"Could not save fault timing: {exc}")
                    fault_report = None
                screen.fill((18, 20, 24))
                screen.blit(font.render(fault_message, True, (235, 75, 75)), (16, 32))
                screen.blit(
                    small_font.render("Failsafe is latched. Close and restart while disarmed.", True, (220, 220, 220)),
                    (16, 82),
                )
                pygame.display.flip()
                time.sleep(0.02)
                continue

            raw_roll = axis(pad, args.roll_axis)
            raw_pitch = axis(pad, args.pitch_axis)
            raw_yaw = axis(pad, args.yaw_axis)
            left = trigger(axis(pad, args.left_trigger_axis), left_trigger_rest)
            right = trigger(axis(pad, args.right_trigger_axis), right_trigger_rest)

            roll = 1500 + int(deadzone(raw_roll) * ROLL_PITCH_RANGE)
            pitch = 1500 - int(deadzone(raw_pitch) * ROLL_PITCH_RANGE)
            yaw = 1500 + int(deadzone(raw_yaw) * YAW_RANGE)

            if link.status == "ARMED" and link.link_ok:
                armed_confirmed = True
                pending_arm_since = None
                throttle_us = throttle_target(args.hover_throttle, left, right, args.climb_range)
            else:
                throttle_us = 1000

            link.set_sticks(roll=roll, pitch=pitch, throttle=throttle_us, yaw=yaw)
            if link.paused:
                # Initial/manual pause only: watchdog failures are latched and
                # displayed above before the loop can resume.
                link.resume()

            disarm_pressed = button(pad, args.disarm_button)
            if disarm_pressed and not previous_disarm:
                link.disarm()
                throttle_us = 1000
                armed_confirmed = False
                pending_arm_since = None
                print("DISARM requested")
            previous_disarm = disarm_pressed

            kill_pressed = button(pad, args.kill_button)
            if kill_pressed and not previous_kill:
                link.kill()
                throttle_us = 1000
                armed_confirmed = False
                fault_message = "KILL SENT - CONTROL STOPPED"
                print("KILL sent; failsafe latched. Close and restart while disarmed.")
            previous_kill = kill_pressed

            if fault_message is not None:
                continue

            start_pressed = button(pad, args.arm_button)
            controls_neutral = controls_are_neutral(
                raw_roll, raw_pitch, raw_yaw, left, right
            )

            if armed_confirmed:
                arm_hold_started = None
                arm_hold_fired = True
                if start_pressed and not previous_start:
                    link.disarm()
                    throttle_us = 1000
                    armed_confirmed = False
                    pending_arm_since = None
                    print("DISARM requested by Start")
            elif (
                start_pressed
                and not arm_hold_fired
                and link.status == "DISARMED"
                and link.link_ok
                and controls_neutral
            ):
                if arm_hold_started is None:
                    arm_hold_started = now
                if now - arm_hold_started >= ARM_HOLD_SECONDS:
                    if link.arm():
                        pending_arm_since = now
                        arm_hold_fired = True
                        print("ARM requested after 3-second Start hold")
                    else:
                        print(f"Arm refused: state={link.status}, throttle={throttle_us}")
                        arm_hold_fired = True
            elif not start_pressed:
                arm_hold_started = None
                arm_hold_fired = False
            else:
                arm_hold_started = None

            if pending_arm_since is not None and now - pending_arm_since > 1.0:
                print("Arm acknowledgement timed out; returning to DISARMED.")
                link.disarm()
                pending_arm_since = None

            previous_start = start_pressed

            status = link.status if link.link_ok else "NO LINK"
            if arm_hold_started is not None:
                remaining = max(0.0, ARM_HOLD_SECONDS - (now - arm_hold_started))
                status = f"HOLD START {remaining:.1f}s"
            elif pending_arm_since is not None:
                status = "ARMING..."
            if now - last_display >= 0.25:
                print(
                    f"\r{status:18} R {roll:4} P {pitch:4} T {throttle_us:4} Y {yaw:4}",
                    end="",
                    flush=True,
                )
                last_display = now

            screen.fill((18, 20, 24))
            color = (75, 210, 120) if status in ("DISARMED", "ARMED") else (235, 175, 65)
            if status == "NO LINK":
                color = (235, 75, 75)
            lines = (
                (font, f"{status}    R {roll}  P {pitch}  T {throttle_us}  Y {yaw}", color),
                (small_font, "Hold Start 3s: ARM    Start/B: DISARM    Back: KILL", (220, 220, 220)),
                (small_font, "Left stick: roll/pitch    Right X: yaw    RT/LT: climb/descend", (160, 168, 180)),
                (small_font, f"Neutral throttle: {args.hover_throttle}    Closing window invokes failsafe.", (160, 168, 180)),
            )
            y = 16
            for line_font, text, line_color in lines:
                screen.blit(line_font.render(text, True, line_color), (16, y))
                y += 34
            pygame.display.flip()

            time.sleep(0.005)
    finally:
        # DroneLink.stop sends a final disarm burst before closing, while
        # the ESP independently handles packet loss with the same safe values.
        link.stop()
        pygame.quit()
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
