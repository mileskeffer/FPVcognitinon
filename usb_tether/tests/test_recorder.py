import json
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from msp import MSP_ATTITUDE, MSP_MOTOR, MspLink
from tether_control import shape
from tests.test_msp import response


class FakeSerial:
    def __init__(self, data):
        self.data = data
        self.in_waiting = len(data)

    def read(self, _count):
        data, self.data, self.in_waiting = self.data, b"", 0
        return data


class RecorderTests(unittest.TestCase):
    def test_shape_keeps_endpoints_and_softens_centre(self):
        self.assertEqual(shape(0.0, 0.4), 0.0)
        self.assertAlmostEqual(shape(1.0, 0.4), 1.0)
        self.assertAlmostEqual(shape(-1.0, 0.4), -1.0)
        self.assertLess(shape(0.5, 0.4), shape(0.5, 0.0))

    def test_attitude_and_motor_replies(self):
        link = MspLink.__new__(MspLink)
        from msp import FrameParser
        link._parser = FrameParser()
        link.serial = FakeSerial(response(MSP_ATTITUDE, struct.pack("<hhh", -123, 45, 270))
                                 + response(MSP_MOTOR, struct.pack("<8H", *range(1100, 1900, 100))))
        link._read_replies()
        self.assertEqual(link.attitude, (-12.3, 4.5, 270))
        self.assertEqual(link.motors[:4], (1100, 1200, 1300, 1400))

    def test_analyzer_flags_uncommanded_tilt(self):
        flight = []
        for i in range(40):
            tilt = 25.0 if 20 <= i < 24 else 1.0
            flight.append({"t": i * 0.05, "state": "ARMED", "sent": [1500, 1500, 1300, 1500, 1],
                           "att": [tilt, 0.0, 90], "motors": [1300, 1310, 1290, 1300]})
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "tether-flight-test.json"
            log.write_text(json.dumps({"flight": flight}))
            out = subprocess.run([sys.executable, str(HERE / "analyze_flight.py"), str(log)],
                                 capture_output=True, text=True, check=True).stdout
        self.assertIn("1 uncommanded tilt event", out)
        self.assertIn("peak 25.0 deg", out)


if __name__ == "__main__":
    unittest.main()


class TiltCutoffTests(unittest.TestCase):
    def make_link(self, attitude):
        link = MspLink.__new__(MspLink)
        link.watchdog_error = None
        link.last_reply = __import__("time").monotonic()
        link.attitude = attitude
        return link

    def test_disarms_past_max_tilt_only_when_armed(self):
        from tether_control import link_fault
        flipped = self.make_link((-27.6, -65.8, 277))  # 2026-10-04 05:01 log, t=7.88 s
        self.assertIn("TILT 71 DEG", link_fault(flipped, True, 70.0))
        self.assertIsNone(link_fault(flipped, False, 70.0))
        self.assertIsNone(link_fault(flipped, True, 0.0))
        self.assertIsNone(link_fault(self.make_link((-5.6, -17.3, 312)), True, 70.0))
