import unittest
import threading
from types import SimpleNamespace
from unittest import mock

from gamepad_control import (background_job, control_link_fault, controls_are_neutral,
                             deadzone, read_receiver_diagnostics, throttle_target, trigger)


class GamepadMappingTests(unittest.TestCase):
    def test_slow_diagnostics_do_not_block_the_caller(self):
        entered = threading.Event()
        release = threading.Event()

        def slow_receiver(*_args, **_kwargs):
            entered.set()
            release.wait(2.0)
            return {"firmware": "air75-diag-1"}

        with mock.patch("gamepad_control.fetch_diagnostics", side_effect=slow_receiver):
            result = background_job(read_receiver_diagnostics, "192.168.4.1", 0.7)
            try:
                self.assertTrue(entered.wait(1.0))
                self.assertFalse(result.done())
            finally:
                release.set()
            self.assertEqual(result.result(timeout=1.0), {"firmware": "air75-diag-1"})

    def test_unreachable_receiver_diagnostics_return_an_error(self):
        with mock.patch("gamepad_control.fetch_diagnostics", side_effect=TimeoutError("no link")):
            result = background_job(read_receiver_diagnostics, "192.168.4.1")
            self.assertEqual(result.result(timeout=1.0), {"error": "no link"})

    def test_watchdog_latches_even_when_receiver_ack_is_fresh(self):
        link = SimpleNamespace(watchdog_error="CONTROL LOOP STALLED (300 ms)",
                               link_ok=True, status="DISARMED")
        self.assertEqual(control_link_fault(link, False), link.watchdog_error)

    def test_receiver_failsafe_is_reported_with_fresh_ack(self):
        link = SimpleNamespace(watchdog_error=None, link_ok=True, status="FAILSAFE_LATCHED")
        self.assertEqual(control_link_fault(link, True), "RECEIVER FAILSAFE - CONTROL STOPPED")

    def test_initial_no_link_waits_but_armed_ack_loss_stops(self):
        link = SimpleNamespace(watchdog_error=None, link_ok=False, status="NO LINK")
        self.assertIsNone(control_link_fault(link, False))
        self.assertEqual(control_link_fault(link, True), "ACKNOWLEDGEMENTS LOST - CONTROL STOPPED")

    def test_deadzone(self):
        self.assertEqual(deadzone(0.04), 0.0)
        self.assertEqual(deadzone(-0.08), 0.0)
        self.assertGreater(deadzone(0.5), 0.0)
        self.assertLess(deadzone(-0.5), 0.0)

    def test_trigger_normalization(self):
        self.assertEqual(trigger(-1.0, -1.0), 0.0)
        self.assertEqual(trigger(1.0, -1.0), 1.0)
        self.assertEqual(trigger(0.0, 0.0), 0.0)
        self.assertEqual(trigger(1.0, 0.0), 1.0)

    def test_hover_centered_throttle(self):
        self.assertEqual(throttle_target(1300, 0.0, 0.0, 500), 1300)
        self.assertEqual(throttle_target(1300, 1.0, 0.0, 500), 1000)
        self.assertEqual(throttle_target(1300, 0.0, 1.0, 500), 1800)
        self.assertEqual(throttle_target(1000, 0.0, 0.5, 500), 1250)

    def test_arm_neutral_detection(self):
        self.assertTrue(controls_are_neutral(0.0, 0.0, 0.0, 0.0, 0.0))
        self.assertFalse(controls_are_neutral(0.2, 0.0, 0.0, 0.0, 0.0))
        self.assertFalse(controls_are_neutral(0.0, 0.0, 0.0, 0.0, 0.2))


if __name__ == "__main__":
    unittest.main()
