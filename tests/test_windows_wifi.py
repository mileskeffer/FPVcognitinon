import unittest
from types import SimpleNamespace
from unittest import mock

from windows_wifi import BACKGROUND_SCAN, MEDIA_STREAMING, WifiControlSession


class WifiControlSessionTests(unittest.TestCase):
    def setUp(self):
        self.platform = mock.patch("windows_wifi.sys.platform", "win32")
        self.platform.start()
        self.addCleanup(self.platform.stop)
        self.factory_patch = mock.patch("windows_wifi._WlanApi")
        self.api = self.factory_patch.start().return_value
        self.addCleanup(self.factory_patch.stop)
        self.home = SimpleNamespace(state=1, description="Internet adapter")
        self.drone = SimpleNamespace(state=1, description="Drone adapter")
        self.offline = SimpleNamespace(state=4, description="Disconnected")
        self.api.interfaces.return_value = [self.home, self.drone, self.offline]
        self.api.ssid.side_effect = [b"Home", b"Air75-Control"]

    def test_only_drone_adapter_is_tuned_and_requests_are_released(self):
        session = WifiControlSession()
        session.start()
        self.assertIn("background scans off", session.message)
        self.assertEqual(self.api.set_bool.call_args_list, [
            mock.call(self.drone, BACKGROUND_SCAN, False),
            mock.call(self.drone, MEDIA_STREAMING, True),
        ])
        session.start()  # Already active: don't acquire another handle.
        self.api.interfaces.assert_called_once()
        session.close()
        session.close()
        self.assertEqual(self.api.set_bool.call_args_list[-2:], [
            mock.call(self.drone, MEDIA_STREAMING, False),
            mock.call(self.drone, BACKGROUND_SCAN, True),
        ])
        self.api.close.assert_called_once()

    def test_partial_driver_support_keeps_and_releases_successful_request(self):
        self.api.set_bool.side_effect = [None, OSError("unsupported"), None]
        session = WifiControlSession()
        session.start()
        self.assertIn("incomplete", session.message)
        self.api.close.assert_not_called()
        session.close()
        self.api.set_bool.assert_called_with(self.drone, BACKGROUND_SCAN, True)
        self.api.close.assert_called_once()

    def test_permission_error_does_not_prevent_control_or_leak_handle(self):
        self.api.interfaces.side_effect = OSError("access denied")
        session = WifiControlSession()
        session.start()
        self.assertIn("access denied", session.message)
        self.api.set_bool.assert_not_called()
        self.api.close.assert_called_once()

    def test_other_network_is_never_modified(self):
        self.api.interfaces.return_value = [self.home, self.offline]
        session = WifiControlSession()
        session.start()
        self.api.set_bool.assert_not_called()
        self.api.close.assert_called_once()
        self.assertIn("connect to Air75-Control", session.message)

    def test_disconnect_during_cleanup_still_closes_handle(self):
        session = WifiControlSession()
        session.start()
        self.api.set_bool.side_effect = OSError("disconnected")
        session.close()
        self.api.close.assert_called_once()

    def test_non_windows_does_not_load_wlan_library(self):
        with mock.patch("windows_wifi.sys.platform", "linux"):
            session = WifiControlSession()
            session.start()
            session.close()
        self.api.interfaces.assert_not_called()


if __name__ == "__main__":
    unittest.main()
