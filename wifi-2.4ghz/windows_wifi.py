"""Temporary Windows Wi-Fi settings for the connected drone adapter.

Suppress background scans during control and release this client's requests on
shutdown. No registry or persistent adapter settings are changed.
https://learn.microsoft.com/windows/win32/api/wlanapi/ne-wlanapi-wlan_intf_opcode
"""

from __future__ import annotations

import ctypes
import sys

BACKGROUND_SCAN = 2
MEDIA_STREAMING = 3
CURRENT_CONNECTION = 7


class _Guid(ctypes.Structure):
    _fields_ = [("data1", ctypes.c_uint32), ("data2", ctypes.c_uint16),
                ("data3", ctypes.c_uint16), ("data4", ctypes.c_ubyte * 8)]


class _Interface(ctypes.Structure):
    _fields_ = [("guid", _Guid), ("description", ctypes.c_wchar * 256),
                ("state", ctypes.c_uint32)]


class _InterfaceListHeader(ctypes.Structure):
    _fields_ = [("count", ctypes.c_uint32), ("index", ctypes.c_uint32)]


class _Ssid(ctypes.Structure):
    _fields_ = [("length", ctypes.c_uint32), ("data", ctypes.c_ubyte * 32)]


class _ConnectionPrefix(ctypes.Structure):
    _fields_ = [("state", ctypes.c_uint32), ("mode", ctypes.c_uint32),
                ("profile", ctypes.c_wchar * 256), ("ssid", _Ssid)]


class _WlanApi:
    def __init__(self):
        self.dll = ctypes.WinDLL("wlanapi")
        pointer = ctypes.c_void_p
        dword = ctypes.c_uint32
        for name, args in {
            "WlanOpenHandle": [dword, pointer, ctypes.POINTER(dword), ctypes.POINTER(pointer)],
            "WlanEnumInterfaces": [pointer, pointer, ctypes.POINTER(pointer)],
            "WlanQueryInterface": [pointer, ctypes.POINTER(_Guid), dword, pointer,
                                   ctypes.POINTER(dword), ctypes.POINTER(pointer), pointer],
            "WlanSetInterface": [pointer, ctypes.POINTER(_Guid), dword, dword, pointer, pointer],
            "WlanCloseHandle": [pointer, pointer],
        }.items():
            function = getattr(self.dll, name)
            function.argtypes = args
            function.restype = dword
        self.dll.WlanFreeMemory.argtypes = [pointer]
        self.dll.WlanFreeMemory.restype = None
        self.handle = pointer()
        version = dword()
        self._check(self.dll.WlanOpenHandle(2, None, ctypes.byref(version), ctypes.byref(self.handle)))

    @staticmethod
    def _check(code):
        if code:
            raise ctypes.WinError(code)

    def interfaces(self):
        data = ctypes.c_void_p()
        self._check(self.dll.WlanEnumInterfaces(self.handle, None, ctypes.byref(data)))
        try:
            count = ctypes.cast(data, ctypes.POINTER(_InterfaceListHeader)).contents.count
            address = data.value + ctypes.sizeof(_InterfaceListHeader)
            # Copy records before freeing the WLAN-owned buffer.
            return [_Interface.from_buffer_copy(ctypes.string_at(
                address + i * ctypes.sizeof(_Interface), ctypes.sizeof(_Interface)
            )) for i in range(count)]
        finally:
            self.dll.WlanFreeMemory(data)

    def ssid(self, interface):
        data = ctypes.c_void_p()
        size = ctypes.c_uint32()
        self._check(self.dll.WlanQueryInterface(
            self.handle, ctypes.byref(interface.guid), CURRENT_CONNECTION, None,
            ctypes.byref(size), ctypes.byref(data), None,
        ))
        try:
            if size.value < ctypes.sizeof(_ConnectionPrefix):
                raise OSError("Incomplete WLAN connection information")
            ssid = ctypes.cast(data, ctypes.POINTER(_ConnectionPrefix)).contents.ssid
            if ssid.length > 32:
                raise OSError("Invalid WLAN SSID length")
            return bytes(ssid.data[:ssid.length])
        finally:
            self.dll.WlanFreeMemory(data)

    def set_bool(self, interface, opcode, enabled):
        value = ctypes.c_int32(enabled)
        self._check(self.dll.WlanSetInterface(
            self.handle, ctypes.byref(interface.guid), opcode,
            ctypes.sizeof(value), ctypes.byref(value), None,
        ))

    def close(self):
        if self.handle:
            self.dll.WlanCloseHandle(self.handle, None)
            self.handle = None


class WifiControlSession:
    """Best-effort scan suppression for the connected Air75 SSID only."""

    def __init__(self, ssid="Air75-Control"):
        self.ssid = ssid.encode("utf-8")
        self.message = ""
        self._api = None
        self._requests = []

    def start(self):
        if sys.platform != "win32" or self._api is not None:
            return
        try:
            self._api = _WlanApi()
            errors = []
            matched = False
            for interface in self._api.interfaces():
                if interface.state != 1:  # wlan_interface_state_connected
                    continue
                try:
                    if self._api.ssid(interface) != self.ssid:
                        continue
                except OSError as exc:
                    errors.append(str(exc))
                    continue
                matched = True
                for opcode, enabled in ((BACKGROUND_SCAN, False), (MEDIA_STREAMING, True)):
                    try:
                        self._api.set_bool(interface, opcode, enabled)
                        self._requests.append((interface, opcode, not enabled))
                    except OSError as exc:
                        errors.append(str(exc))
            if errors:
                self.message = "Wi-Fi scan/streaming setup incomplete: " + "; ".join(errors)
            elif matched:
                self.message = "Air75 Wi-Fi: background scans off, streaming mode on during control"
            else:
                self.message = "Wi-Fi tuning unavailable: connect to Air75-Control before starting"
            if not self._requests:
                self.close()
        except OSError as exc:
            self.message = f"Wi-Fi scan/streaming setup unavailable: {exc}"
            self.close()

    def close(self):
        if self._api is None:
            return
        try:
            # Withdraw this handle's requests; Windows combines requests from
            # all clients, retaining any other application's preference.
            for interface, opcode, enabled in reversed(self._requests):
                try:
                    self._api.set_bool(interface, opcode, enabled)
                except OSError:
                    pass  # A disconnect already resets these transient modes.
        finally:
            self._requests.clear()
            self._api.close()
            self._api = None
