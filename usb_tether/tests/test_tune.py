import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tune_betaflight import airmode_enabled, parse_setting


class TuneParsingTests(unittest.TestCase):
    def test_reads_setting_value(self):
        output = "get angle_p_gain\r\nangle_p_gain = 50\r\nAllowed range: 0 - 200\r\n\r\n# "
        self.assertEqual(parse_setting(output, "angle_p_gain"), 50)
        self.assertIsNone(parse_setting(output, "angle_strength"))

    def test_airmode_only_counts_enabled_list(self):
        on = "Enabled: RX_MSP OSD AIRMODE ANTI_GRAVITY\r\nAvailable: RX_PPM AIRMODE\r\n"
        off = "Enabled: RX_MSP OSD ANTI_GRAVITY\r\nAvailable: RX_PPM AIRMODE\r\n"
        self.assertTrue(airmode_enabled(on))
        self.assertFalse(airmode_enabled(off))


if __name__ == "__main__":
    unittest.main()
