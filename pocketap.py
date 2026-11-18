"""PocketAP - Wi-Fi hotspot for Windows that runs even without an internet connection.

Usage:  python pocketap.py [SSID] [-p PASSWORD]
While running, it is controlled by console commands. The help command lists them.

The hotspot runs on Wi-Fi Direct Legacy AP. Windows (ICS) assigns client addresses,
and when it does not, a built-in DHCP server starts (this requires administrator rights).
Internet sharing is off by default, the command internet on turns it on.
Missing WinRT packages are installed automatically on first launch.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import importlib
import ipaddress
import json
import os
import select
import shutil
import site
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
from ctypes import wintypes
from pathlib import Path

WINRT_VERSION = "4!10.0.28000.2957"
WINRT_PACKAGES = [
    "winrt-runtime==4.2.0",
    *(
        f"winrt-Windows.{name}=={WINRT_VERSION}"
        for name in (
            "Devices.WiFiDirect", "Devices.Enumeration", "Foundation", "Foundation.Collections",
            "Networking", "Security.Credentials", "Storage.Streams",
        )
    ),
]


def exit_with_error(message: str) -> None:
    """Print an error and keep the window open so it can be read after a double-click launch."""
    print(message, flush=True)
    try:
        input("Press Enter to close this window...")
    except (EOFError, OSError, RuntimeError):
        pass
    sys.exit(1)


def load_winrt():
    for name in (
        "devices.enumeration", "foundation", "foundation.collections", "networking", "security.credentials",
    ):
        importlib.import_module(f"winrt.windows.{name}")
    return importlib.import_module("winrt.windows.devices.wifidirect")


def install_winrt() -> None:
    print("Installing required packages, the first launch needs internet access...", flush=True)
    command = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *WINRT_PACKAGES]
    if subprocess.run(command, check=False).returncode != 0:
        exit_with_error("Could not install the packages. Internet access and Python 3.11 or newer are required.")
    site.addsitedir(site.getusersitepackages())  # The folder may have been created by this install.
    importlib.invalidate_caches()


try:
    wd = load_winrt()
except ImportError:
    install_winrt()
    wd = load_winrt()

DEFAULT_SSID = "PocketAP"
DEFAULT_PASSWORD = "123456789"
RETRY_SECONDS = 3
FIREWALL_RULE = "PocketAP DHCP"
LEASE_SECONDS = 12 * 3600
WIFI_POWER_SUBGROUP = "19cbb8fa-5279-450e-9fac-8a3d5fedd0c1"
WIFI_POWER_SETTING = "12bbebe6-58d6-4636-95bb-3217ef867c1a"  # Wi-Fi power saving, 0 is maximum performance.
WLAN_BACKGROUND_SCAN, WLAN_STREAMING_MODE = 2, 3  # WLAN_INTF_OPCODE

ADAPTERS_PS = r"""
$ips = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue)
$dhcp = @(Get-NetUDPEndpoint -LocalPort 67 -ErrorAction SilentlyContinue | ForEach-Object { $_.LocalAddress })
Get-NetAdapter -IncludeHidden | Where-Object {
  $_.Status -eq 'Up' -and ($_.InterfaceDescription -like '*Wi-Fi Direct*' -or $_.ComponentID -like '*vwifimp_wfd*')
} | ForEach-Object {
  $index = [int]$_.ifIndex
  $own = @($ips | Where-Object { $_.InterfaceIndex -eq $index } | ForEach-Object { $_.IPAddress })
  [pscustomobject]@{ Index = $index; Mac = $_.MacAddress; Dhcp = @($own | Where-Object { $dhcp -contains $_ }) }
}
"""

SHARING_PS = r"""
$ErrorActionPreference = 'Stop'
$net = New-Object -ComObject HNetCfg.HNetShare
function Get-SharingConfig {
  foreach ($item in @($net.EnumEveryConnection)) {
    try {
      [pscustomobject]@{
        Guid = $net.NetConnectionProps.Invoke($item).Guid
        Config = $net.INetSharingConfigurationForINetConnection.Invoke($item)
      }
    } catch {}
  }
}
function Stop-AllSharing {
  Get-SharingConfig | Where-Object { $_.Config.SharingEnabled } | ForEach-Object { $_.Config.DisableSharing() }
}
"""

START_SHARING_PS = r"""
try {
  $route = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -PolicyStore ActiveStore -ErrorAction SilentlyContinue |
    Where-Object { $_.InterfaceIndex -ne $hotspot } |
    Sort-Object { $_.RouteMetric + (Get-NetIPInterface -InterfaceIndex $_.InterfaceIndex -AddressFamily IPv4).InterfaceMetric } |
    Select-Object -First 1
  if (-not $route) { throw 'This computer is not connected to the internet right now.' }
  $upstream = Get-NetAdapter -IncludeHidden -InterfaceIndex $route.InterfaceIndex
  $guid = (Get-NetAdapter -IncludeHidden -InterfaceIndex $hotspot).InterfaceGuid
  $lan = $null
  foreach ($attempt in 1..20) {
    $lan = Get-SharingConfig | Where-Object { $_.Guid -eq $guid } | Select-Object -First 1
    if ($lan) { break }
    Start-Sleep -Milliseconds 500
  }
  $wan = Get-SharingConfig | Where-Object { $_.Guid -eq $upstream.InterfaceGuid } | Select-Object -First 1
  if (-not $lan -or -not $wan) { throw 'Windows does not support sharing on this connection.' }
  Stop-AllSharing
  $wan.Config.EnableSharing(0)
  $lan.Config.EnableSharing(1)
  $ip = $null
  foreach ($attempt in 1..20) {
    $ip = Get-NetIPAddress -InterfaceIndex $hotspot -AddressFamily IPv4 -ErrorAction SilentlyContinue |
      Where-Object { $_.IPAddress -notlike '169.254.*' } | Select-Object -First 1 -ExpandProperty IPAddress
    if ($ip) { break }
    Start-Sleep -Milliseconds 500
  }
  if (-not $ip) { throw 'The hotspot did not get an address from Windows sharing.' }
  [pscustomobject]@{ Ip = $ip; Upstream = $upstream.Name }
} catch {
  $message = $_.Exception.Message
  try { Stop-AllSharing } catch {}
  [pscustomobject]@{ Error = $message }
}
"""

STOP_SHARING_PS = r"""
try { Stop-AllSharing } catch { [pscustomobject]@{ Error = $_.Exception.Message } }
"""


class HotspotError(RuntimeError):
    """Error with a message for the user. The hotspot tries to start again after it."""


class NeedsAdmin(Exception):
    """The requested setup requires administrator rights."""


PROMPT = "pocketap> "
HELP = """Commands:
  status                 show the hotspot status
  ssid NAME              change the network name
  password NEW_PASSWORD  change the password (8 to 63 ASCII characters)
  internet on|off        turn internet sharing on or off (default is off)
  stop                   pause the hotspot
  start                  start the hotspot again
  help                   show this help
  exit                   turn off the hotspot and quit

Changes last only until the application exits."""

_output_lock = threading.Lock()
_last_message = None
_at_prompt = False


def say(text: str = "") -> None:
    """Print the reply to a command."""
    with _output_lock:
        print(text, flush=True)


def log(text: str) -> None:
    """Print a timestamped event above the prompt line. Repeated messages are skipped."""
    global _last_message
    with _output_lock:
        if text == _last_message:
            return
        _last_message = text
        line = f"[{time.strftime('%H:%M:%S')}] {text}\n"
        if _at_prompt:
            blank = " " * (shutil.get_terminal_size().columns - 1)
            line = f"\r{blank}\r{line}{PROMPT}"
        print(line, end="", flush=True)


def run(*args: str, timeout: float = 30, encoding: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, capture_output=True, text=True, encoding=encoding, errors="replace", timeout=timeout,
        creationflags=0x08000000,
    )


def powershell_json(script: str, timeout: float = 30) -> list:
    command = (
        "[Console]::OutputEncoding = New-Object Text.UTF8Encoding $false; "
        f"ConvertTo-Json -Compress -Depth 4 -InputObject @({script})"
    )
    try:
        output = run(
            "powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command,
            timeout=timeout, encoding="utf-8",
        ).stdout
        return json.loads(output) if output.strip() else []
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise HotspotError(f"Could not read the network adapter status: {exc}") from exc


def adapters() -> dict[int, dict]:
    """Active Wi-Fi Direct virtual adapters by interface index."""
    return {item["Index"]: item for item in powershell_json(ADAPTERS_PS)}


def is_admin() -> bool:
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def relaunch_as_admin(ssid: str, password: str, internet: bool) -> bool:
    """Relaunch the application through UAC. Returns False when the user declines the prompt."""
    script = Path(__file__).resolve()
    options = [f"--password={password}", *(["--internet"] if internet else [])]
    parameters = subprocess.list2cmdline([str(script), *options, "--", ssid])
    return ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, parameters, str(script.parent), 1) > 32


def quietly(action) -> None:
    """Cleanup must continue even when one step fails."""
    try:
        action()
    except OSError:
        pass


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def parse(cls, text: str) -> GUID:
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)


class WlanInterfaceInfo(ctypes.Structure):
    _fields_ = [("guid", GUID), ("description", ctypes.c_wchar * 256), ("state", ctypes.c_int)]


class WlanInterfaceList(ctypes.Structure):
    _fields_ = [("count", wintypes.DWORD), ("index", wintypes.DWORD), ("items", WlanInterfaceInfo * 1)]


class IfRow(ctypes.Structure):
    """MIB_IF_ROW2, only the interface status is read from it."""

    _fields_ = [
        ("InterfaceLuid", ctypes.c_uint64), ("InterfaceIndex", wintypes.ULONG), ("InterfaceGuid", GUID),
        ("Alias", ctypes.c_wchar * 257), ("Description", ctypes.c_wchar * 257),
        ("PhysicalAddressLength", wintypes.ULONG), ("PhysicalAddress", ctypes.c_ubyte * 32),
        ("PermanentPhysicalAddress", ctypes.c_ubyte * 32), ("Mtu", wintypes.ULONG), ("Type", wintypes.ULONG),
        ("TunnelType", ctypes.c_int), ("MediaType", ctypes.c_int), ("PhysicalMediumType", ctypes.c_int),
        ("AccessType", ctypes.c_int), ("DirectionType", ctypes.c_int), ("Flags", ctypes.c_ubyte),
        ("OperStatus", ctypes.c_int), ("AdminStatus", ctypes.c_int), ("MediaConnectState", ctypes.c_int),
        ("NetworkGuid", GUID), ("ConnectionType", ctypes.c_int), ("Counters", ctypes.c_uint64 * 20),
    ]


def interface_up(if_index: int) -> bool:
    """Interface status straight from Windows, without PowerShell or querying driver statistics."""
    row = IfRow(InterfaceIndex=if_index)
    if ctypes.windll.iphlpapi.GetIfEntry2Ex(2, ctypes.byref(row)):  # MibIfEntryNormalWithoutStatistics
        return False
    return row.OperStatus == 1  # IfOperStatusUp


def check_win32(code: int) -> None:
    if code:
        raise ctypes.WinError(code)


class LowLatency:
    """Turns off what delays Wi-Fi packets while the hotspot runs. stop() restores everything."""

    def __init__(self) -> None:
        self.wlan: wintypes.HANDLE | None = None
        self.interfaces: list[GUID] = []
        self.power: tuple[GUID, int, int] | None = None

    def start(self) -> None:
        done, failed = [], []
        for name, action in (("Wi-Fi power saving off", self._max_power), ("network scanning off", self._quiet_wlan)):
            try:
                action()
                done.append(name)
            except OSError as exc:
                failed.append(f"{name} ({exc.strerror or exc})")
        if done:
            log(f"Low latency: {', '.join(done)}.")
        if failed:
            log(f"Could not apply: {', '.join(failed)}.")

    def stop(self) -> None:
        if self.wlan is not None:
            wlanapi = ctypes.windll.wlanapi
            on, off = wintypes.BOOL(True), wintypes.BOOL(False)
            for guid in self.interfaces:
                wlanapi.WlanSetInterface(self.wlan, ctypes.byref(guid), WLAN_STREAMING_MODE, 4, ctypes.byref(off), None)
                wlanapi.WlanSetInterface(self.wlan, ctypes.byref(guid), WLAN_BACKGROUND_SCAN, 4, ctypes.byref(on), None)
            wlanapi.WlanCloseHandle(self.wlan, None)  # Closing the handle would also make Windows restore these settings.
            self.wlan, self.interfaces = None, []
        if self.power is not None:
            scheme, ac, dc = self.power
            self.power = None
            try:
                self._write_power(scheme, ac, dc)
            except OSError as exc:
                log(f"Could not restore Wi-Fi power saving: {exc.strerror or exc}")

    def _max_power(self) -> None:
        """Power saving lets the card sleep between packets, so packets from clients have to wait."""
        powrprof = ctypes.windll.powrprof
        scheme = self._active_scheme()
        subgroup, setting = GUID.parse(WIFI_POWER_SUBGROUP), GUID.parse(WIFI_POWER_SETTING)
        where = (None, ctypes.byref(scheme), ctypes.byref(subgroup), ctypes.byref(setting))
        ac, dc = wintypes.DWORD(), wintypes.DWORD()
        check_win32(powrprof.PowerReadACValueIndex(*where, ctypes.byref(ac)))
        check_win32(powrprof.PowerReadDCValueIndex(*where, ctypes.byref(dc)))
        self.power = (scheme, ac.value, dc.value)
        self._write_power(scheme, 0, 0)

    def _write_power(self, scheme: GUID, ac: int, dc: int) -> None:
        powrprof = ctypes.windll.powrprof
        subgroup, setting = GUID.parse(WIFI_POWER_SUBGROUP), GUID.parse(WIFI_POWER_SETTING)
        where = (None, ctypes.byref(scheme), ctypes.byref(subgroup), ctypes.byref(setting))
        check_win32(powrprof.PowerWriteACValueIndex(*where, ac))
        check_win32(powrprof.PowerWriteDCValueIndex(*where, dc))
        check_win32(powrprof.PowerSetActiveScheme(None, ctypes.byref(self._active_scheme())))  # Applies the change.

    @staticmethod
    def _active_scheme() -> GUID:
        pointer = ctypes.POINTER(GUID)()
        check_win32(ctypes.windll.powrprof.PowerGetActiveScheme(None, ctypes.byref(pointer)))
        try:
            return GUID.from_buffer_copy(bytes(pointer.contents))
        finally:
            ctypes.windll.kernel32.LocalFree(pointer)

    def _quiet_wlan(self) -> None:
        """Network scans pull the card off the hotspot channel and clients cannot transmit meanwhile."""
        wlanapi = ctypes.windll.wlanapi
        handle, version = wintypes.HANDLE(), wintypes.DWORD()
        check_win32(wlanapi.WlanOpenHandle(2, None, ctypes.byref(version), ctypes.byref(handle)))
        self.wlan = handle
        listing = ctypes.POINTER(WlanInterfaceList)()
        check_win32(wlanapi.WlanEnumInterfaces(handle, None, ctypes.byref(listing)))
        try:
            items = ctypes.cast(listing.contents.items, ctypes.POINTER(WlanInterfaceInfo))
            guids = [GUID.from_buffer_copy(bytes(items[i].guid)) for i in range(listing.contents.count)]
        finally:
            wlanapi.WlanFreeMemory(listing)
        on, off = wintypes.BOOL(True), wintypes.BOOL(False)
        code = 2  # ERROR_FILE_NOT_FOUND when there is no Wi-Fi card.
        for guid in guids:
            code = wlanapi.WlanSetInterface(handle, ctypes.byref(guid), WLAN_BACKGROUND_SCAN, 4, ctypes.byref(off), None)
            if code == 0:
                self.interfaces.append(guid)
                # Streaming mode only works while connected to another Wi-Fi network, errors elsewhere are fine.
                wlanapi.WlanSetInterface(handle, ctypes.byref(guid), WLAN_STREAMING_MODE, 4, ctypes.byref(on), None)
        if not self.interfaces:
            raise ctypes.WinError(code)


class AccessPoint:
    """A running Wi-Fi Direct Legacy AP, including accepting clients."""

    def __init__(self) -> None:
        self.publisher = None
        self.status_token = None
        self.listener = None
        self.listener_token = None
        self.devices: dict[str, tuple] = {}
        self.lock = threading.Lock()
        self.answered = threading.Event()
        self.ended = threading.Event()
        self.stopping = False
        self.error: str | None = None

    def start(self, ssid: str, password: str) -> None:
        try:
            self.publisher = wd.WiFiDirectAdvertisementPublisher()
            self.status_token = self.publisher.add_status_changed(self._on_status)
            advertisement = self.publisher.advertisement
            advertisement.is_autonomous_group_owner_enabled = True
            legacy = advertisement.legacy_settings
            legacy.is_enabled = True
            legacy.ssid = ssid
            credential = legacy.passphrase
            credential.password = password
            legacy.passphrase = credential
            self.publisher.start()
            if not self.answered.wait(25):
                raise HotspotError("Windows did not confirm the hotspot start within 25 s.")
            self.check()
            self.listener = wd.WiFiDirectConnectionListener()
            self.listener_token = self.listener.add_connection_requested(self._on_request)
        except OSError as exc:
            raise HotspotError(f"Wi-Fi Direct failed: {exc}") from exc

    def check(self) -> None:
        if self.ended.is_set():
            raise HotspotError(self.error or "Windows stopped the hotspot.")

    def _on_status(self, _sender, args) -> None:
        status = wd.WiFiDirectAdvertisementPublisherStatus(args.status).name
        if status == "ABORTED":
            self.error = self._explain(args.error)
        if status in ("ABORTED", "STOPPED"):
            self.ended.set()
        if status in ("STARTED", "ABORTED", "STOPPED"):
            self.answered.set()

    @staticmethod
    def _explain(error: int) -> str:
        if error == wd.WiFiDirectError.RESOURCE_IN_USE:
            return "Wi-Fi Direct is in use. Turn off Mobile hotspot or Miracast."
        return "Windows interrupted the hotspot. Check that Wi-Fi is on and Airplane mode is off."

    def _on_request(self, _sender, args) -> None:
        try:
            request = args.get_connection_request()
            info = request.device_information
            target = (request, info.id, info.name)
        except OSError:
            return
        threading.Thread(target=self._accept, args=target, daemon=True).start()

    def _accept(self, request, device_id: str, name: str) -> None:
        try:
            device = asyncio.run(self._open_device(device_id))
            if device is None:
                return
            pairs = device.get_connection_endpoint_pairs()
            address = pairs[0].remote_host_name.display_name if len(pairs) else "IP not known yet"
            token = device.add_connection_status_changed(self._on_device_status)
            with self.lock:
                if self.stopping:
                    device.close()
                    return
                self.devices[device_id] = (device, token, name)
            log(f"+ Connected: {name} ({address})")
        except OSError as exc:
            log(f"Could not accept client {name}: {exc}")
        finally:
            quietly(request.close)

    @staticmethod
    async def _open_device(device_id: str):
        return await wd.WiFiDirectDevice.from_id_async(device_id)

    def _on_device_status(self, sender, _args) -> None:
        try:
            if sender.connection_status != wd.WiFiDirectConnectionStatus.DISCONNECTED:
                return
            device_id = sender.device_id
        except OSError:
            return
        with self.lock:
            entry = self.devices.pop(device_id, None)
        if entry:
            self._close_device(entry)
            log(f"- Disconnected: {entry[2]}")

    @staticmethod
    def _close_device(entry: tuple) -> None:
        device, token, _name = entry
        quietly(lambda: device.remove_connection_status_changed(token))
        quietly(device.close)

    def stop(self) -> None:
        with self.lock:
            self.stopping = True
            entries = list(self.devices.values())
            self.devices.clear()
        if self.listener is not None:
            quietly(lambda: self.listener.remove_connection_requested(self.listener_token))
        for entry in entries:
            self._close_device(entry)
        if self.publisher is not None:
            if not self.ended.is_set():
                quietly(self.publisher.stop)
                self.ended.wait(12)
            quietly(lambda: self.publisher.remove_status_changed(self.status_token))


class DhcpServer:
    """Small DHCP server for a /24 network that runs only on the hotspot interface."""

    DISCOVER, OFFER, REQUEST, ACK, NAK, RELEASE = 1, 2, 3, 5, 6, 7

    def __init__(self, host: str, if_index: int) -> None:
        self.host = host
        self.if_index = if_index
        self.pool = [f"{host.rsplit('.', 1)[0]}.{number}" for number in range(100, 201)]
        self.leases: dict[bytes, tuple[str, float]] = {}
        self.stopped = threading.Event()
        self.sock: socket.socket | None = None
        self.thread: threading.Thread | None = None

    def assign(self, mac: bytes, wanted: str | None, seconds: int) -> str | None:
        """Return the current, requested, or first free address."""
        now = time.time()
        self.leases = {key: lease for key, lease in self.leases.items() if lease[1] > now}
        taken = {lease[0] for key, lease in self.leases.items() if key != mac}
        current = self.leases[mac][0] if mac in self.leases else None
        for ip in (current, wanted, *self.pool):
            if ip in self.pool and ip not in taken:
                self.leases[mac] = (ip, now + seconds)
                return ip
        return None

    def packet(self, request: bytes, kind: int, ip: str = "0.0.0.0") -> tuple[bytes, str]:
        """Build a reply and work out where to send it."""
        host = socket.inet_aton(self.host)
        options = bytes((53, 1, kind, 54, 4)) + host
        if kind != self.NAK:
            options += bytes((1, 4, 255, 255, 255, 0, 28, 4)) + host[:3] + b"\xff"
            options += bytes((51, 4)) + struct.pack("!I", LEASE_SECONDS)
        client_ip = request[12:16] if kind == self.ACK else bytes(4)
        header = (
            bytes((2, 1, 6, 0)) + request[4:8] + bytes(2) + request[10:12] + client_ip
            + socket.inet_aton(ip) + (bytes(4) if kind == self.NAK else host) + request[24:44] + bytes(192)
        )
        body = header + b"\x63\x82\x53\x63" + options + b"\xff"
        target = socket.inet_ntoa(client_ip) if client_ip != bytes(4) else "255.255.255.255"
        return body.ljust(300, b"\x00"), target

    def reply(self, data: bytes) -> tuple[bytes, str] | None:
        """Process a client message and return a reply when one is due."""
        valid = len(data) >= 240 and data[:3] == b"\x01\x01\x06" and data[236:240] == b"\x63\x82\x53\x63"
        if not valid or data[24:28] != bytes(4):
            return None
        options = self._parse_options(data[240:])
        kind = options.get(53, b"\x00")[0]
        mac = data[28:34]
        wanted = socket.inet_ntoa(options[50]) if 50 in options else None
        if wanted is None and data[12:16] != bytes(4):
            wanted = socket.inet_ntoa(data[12:16])

        if kind == self.DISCOVER:
            ip = self.assign(mac, wanted, 60)
            return self.packet(data, self.OFFER, ip) if ip else None
        if kind == self.REQUEST and wanted:
            if options.get(54, socket.inet_aton(self.host)) != socket.inet_aton(self.host):
                return None
            ip = self.assign(mac, wanted, LEASE_SECONDS)
            return self.packet(data, self.ACK, ip) if ip == wanted else self.packet(data, self.NAK)
        if kind == self.RELEASE:
            self.leases.pop(mac, None)
        return None

    @staticmethod
    def _parse_options(data: bytes) -> dict[int, bytes]:
        options: dict[int, bytes] = {}
        index = 0
        while index + 1 < len(data) and data[index] != 255:
            if data[index] == 0:
                index += 1
                continue
            length = data[index + 1]
            options[data[index]] = data[index + 2 : index + 2 + length]
            index += 2 + length
        return options

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.IPPROTO_IP, 31, socket.htonl(self.if_index))  # IP_UNICAST_IF
        for attempt in range(16):  # A new interface address takes a moment to come up.
            try:
                sock.bind((self.host, 67))
                break
            except OSError as exc:
                if attempt == 15:
                    sock.close()
                    raise HotspotError(f"Could not start the DHCP server on {self.host}: {exc}") from exc
                time.sleep(0.5)
        self.sock = sock
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while not self.stopped.is_set():
            try:
                if select.select([self.sock], [], [], 0.5)[0]:
                    answer = self.reply(self.sock.recvfrom(4096)[0])
                    if answer:
                        self.sock.sendto(answer[0], (answer[1], 68))
            except (OSError, IndexError, ValueError):
                continue  # Neither a bad packet nor a socket error stops the server.

    def stop(self) -> None:
        self.stopped.set()
        if self.thread is not None:
            self.thread.join(timeout=3)
        if self.sock is not None:
            self.sock.close()


def free_host_address(if_index: int) -> str:
    """Find a subnet that no other interface on this computer uses."""
    script = (
        "Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
        f"Where-Object {{ $_.InterfaceIndex -ne {if_index} }} | "
        "ForEach-Object { \"$($_.IPAddress)/$($_.PrefixLength)\" }"
    )
    used = [ipaddress.ip_network(item, strict=False) for item in powershell_json(script)]
    for number in range(50, 100):
        candidate = ipaddress.ip_network(f"192.168.{number}.0/24")
        if not any(candidate.overlaps(network) for network in used):
            return f"192.168.{number}.1"
    raise HotspotError("No free subnet found for the hotspot.")


def start_own_dhcp(if_index: int) -> DhcpServer:
    if not is_admin():
        raise NeedsAdmin
    host = free_host_address(if_index)
    result = run(
        "netsh", "interface", "ipv4", "set", "address", f"name={if_index}", "source=static",
        f"address={host}", "mask=255.255.255.0", "gateway=none",
    )
    if result.returncode != 0:
        raise HotspotError(f"Could not set address {host}: {result.stdout.strip()}")
    python = getattr(sys, "_base_executable", None) or sys.executable
    run("netsh", "advfirewall", "firewall", "delete", "rule", f"name={FIREWALL_RULE}")
    run(
        "netsh", "advfirewall", "firewall", "add", "rule", f"name={FIREWALL_RULE}", "dir=in", "action=allow",
        "protocol=UDP", "localport=67", "remoteport=68", "profile=any", "interfacetype=wireless", f"program={python}",
    )
    server = DhcpServer(host, if_index)
    server.start()
    return server


def stop_own_dhcp(server: DhcpServer) -> None:
    server.stop()
    run("netsh", "advfirewall", "firewall", "delete", "rule", f"name={FIREWALL_RULE}")
    run("netsh", "interface", "ipv4", "set", "address", f"name={server.if_index}", "source=dhcp")


def start_sharing(if_index: int) -> tuple[str, str]:
    """Turn on internet sharing into the hotspot. Returns the computer's address and the shared connection name."""
    script = f"$hotspot = {if_index}\n{SHARING_PS}\n{START_SHARING_PS}"
    result = powershell_json(script, timeout=60)
    info = result[0] if result else {}
    if not info.get("Ip"):
        raise HotspotError(info.get("Error") or "Windows did not turn sharing on.")
    return info["Ip"], info["Upstream"]


def stop_sharing() -> None:
    """Hotspot cleanup must not fail, any error is only reported."""
    try:
        result = powershell_json(f"{SHARING_PS}\n{STOP_SHARING_PS}", timeout=60)
    except HotspotError as exc:
        result = [{"Error": str(exc)}]
    if result:
        log(f"Could not turn off internet sharing: {result[0].get('Error')}")


def wait_for_adapter(before: dict[int, str], ap: AccessPoint) -> int:
    """Find the virtual adapter created by starting the hotspot."""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        ap.check()
        for index, adapter in adapters().items():
            if before.get(index) != adapter["Mac"]:
                return index
        time.sleep(0.5)
    raise HotspotError("Windows did not create the hotspot virtual adapter.")


def wait_for_windows_dhcp(if_index: int, ap: AccessPoint, stop: threading.Event) -> str | None:
    """Wait to see whether Windows provides DHCP and return the computer's address on the network."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not stop.is_set():
        ap.check()
        adapter = adapters().get(if_index)
        if adapter is None:
            raise HotspotError("The hotspot virtual adapter disappeared.")
        if adapter["Dhcp"]:
            return adapter["Dhcp"][0]
        time.sleep(1)
    return None


def watch(ap: AccessPoint, if_index: int, stop: threading.Event) -> None:
    """Monitor the hotspot until it ends. An outage raises an error that triggers a restart."""
    seconds = missing = 0
    while not stop.wait(1):
        ap.check()
        seconds += 1
        if seconds % 5:
            continue
        missing = 0 if interface_up(if_index) else missing + 1
        if missing >= 2:
            raise HotspotError("The hotspot virtual adapter went down.")


class Hotspot:
    """Runs the hotspot in the background. The network name and password stay in process memory only."""

    def __init__(self, ssid: str, password: str, internet: bool = False) -> None:
        self.ssid = ssid
        self.password = password
        self.internet = internet
        self.upstream: str | None = None
        self.enabled = True
        self.closing = False
        self.state = "starting"
        self.host: str | None = None
        self.ap: AccessPoint | None = None
        self.changed = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def resume(self) -> None:
        self.enabled = True
        log("Starting hotspot...")
        self.changed.set()

    def pause(self) -> None:
        self.enabled = False
        log("Pausing hotspot...")
        self.changed.set()

    def apply(self, message: str) -> None:
        """A running hotspot restarts with the new settings."""
        if self.enabled:
            log(f"{message} Restarting hotspot...")
            self.changed.set()
        else:
            say(f"{message} Takes effect after the start command.")

    def close(self, timeout: float = 20) -> None:
        self.closing = True
        self.changed.set()
        self.thread.join(timeout)

    def internet_state(self) -> str:
        if self.upstream:
            return f"shared from {self.upstream}"
        return "on, not shared yet" if self.internet else "off"

    def describe(self) -> str:
        ap = self.ap
        return (
            f"  Status:   {self.state}\n  Network:  {self.ssid}\n  Password: {self.password}\n"
            f"  IP:       {self.host or '-'}\n  Clients:  {len(ap.devices) if ap else 0}\n"
            f"  Internet: {self.internet_state()}"
        )

    def _loop(self) -> None:
        while True:
            self.changed.clear()
            if self.closing:
                return
            if not self.enabled:
                self.state = "paused"
                log("The hotspot is paused. The start command runs it again.")
                self.changed.wait()
                continue
            self.state = "starting"
            try:
                self._run()
            except NeedsAdmin:
                self._request_admin()
            except Exception as exc:  # noqa: BLE001 - the hotspot must always try to come back up
                self.state = "error, retrying"
                log(f"{exc} Retrying...")
                self.changed.wait(RETRY_SECONDS)

    def _request_admin(self) -> None:
        if self.internet:
            log("Internet sharing requires administrator rights.")
        else:
            log("Windows provides no DHCP, the built-in server requires administrator rights.")
        if relaunch_as_admin(self.ssid, self.password, self.internet):
            os._exit(0)
        if self.internet:
            self.internet = False
            log("Internet cannot be shared without administrator rights, the hotspot will run without it.")
            return
        log("Clients will not get an IP address without administrator rights. Run the application as administrator.")
        self.enabled = False

    def _share_internet(self, if_index: int) -> str | None:
        """A sharing failure does not bring the hotspot down, it keeps running as a local network."""
        try:
            host, self.upstream = start_sharing(if_index)
            return host
        except HotspotError as exc:
            log(f"Could not share the internet: {exc} The hotspot will run without it.")
            return None

    def _run(self) -> None:
        """One hotspot run. Ends when the requested state changes or on an error."""
        ssid, password, internet = self.ssid, self.password, self.internet
        if internet and not is_admin():
            raise NeedsAdmin
        before = {index: adapter["Mac"] for index, adapter in adapters().items()}
        ap = self.ap = AccessPoint()
        tuning = LowLatency()
        server = None
        if_index = None
        try:
            ap.start(ssid, password)
            if_index = wait_for_adapter(before, ap)
            host = self._share_internet(if_index) if internet else None
            if host is None:
                host = wait_for_windows_dhcp(if_index, ap, self.changed)
            if self.changed.is_set():
                return
            if host is None:
                server = start_own_dhcp(if_index)
                host = server.host
            self.host = host
            self.state = "running"
            shared = f", internet from {self.upstream}" if self.upstream else ""
            log(f"Hotspot running: network {ssid}, password {password}, IP {host}{shared}")
            tuning.start()
            watch(ap, if_index, self.changed)
        finally:
            tuning.stop()
            upstream, self.upstream = self.upstream, None
            self.host = self.ap = None
            if server is not None:
                stop_own_dhcp(server)
            if upstream is not None:
                stop_sharing()
            ap.stop()
            if if_index is not None and not self.closing:
                self._wait_until_down(if_index)

    @staticmethod
    def _wait_until_down(if_index: int) -> None:
        """The next start only recognizes a new adapter once the old one is no longer active."""
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            try:
                if if_index not in adapters():
                    return
            except HotspotError:
                return
            time.sleep(0.5)


def ssid_problem(ssid: str) -> str | None:
    if not 1 <= len(ssid.encode("utf-8")) <= 32 or not ssid.isprintable():
        return "The network name must be 1 to 32 bytes long."
    return None


def password_problem(password: str) -> str | None:
    if not (8 <= len(password) <= 63 and password.isascii() and password.isprintable()):
        return "The password must be 8 to 63 ASCII characters long."
    return None


def execute(hotspot: Hotspot, line: str) -> bool:
    """Run a console command. Returns False when the application should exit."""
    parts = line.strip().split(maxsplit=1)
    if not parts:
        return True
    command, value = parts[0].lower(), parts[1].strip() if len(parts) > 1 else ""

    if command in ("help", "?"):
        say(HELP)
    elif command == "status":
        say(hotspot.describe())
    elif command == "ssid":
        problem = ssid_problem(value)
        if problem:
            say(f"{problem} Usage: ssid NAME")
        elif value == hotspot.ssid:
            say("The network already has this name.")
        else:
            hotspot.ssid = value
            hotspot.apply(f"The network name is now {value}.")
    elif command == "password":
        problem = password_problem(value)
        if problem:
            say(f"{problem} Usage: password NEW_PASSWORD")
        elif value == hotspot.password:
            say("This password is already set.")
        else:
            hotspot.password = value
            hotspot.apply("The password has been changed.")
    elif command == "internet":
        choice = value.lower()
        if choice not in ("on", "off"):
            say(f"Internet sharing: {hotspot.internet_state()}. Usage: internet on|off")
        elif (choice == "on") == hotspot.internet:
            say(f"Internet sharing is already {'on' if hotspot.internet else 'off'}.")
        else:
            hotspot.internet = choice == "on"
            if hotspot.internet and hotspot.password == DEFAULT_PASSWORD:
                say("Warning: the network uses the default password, anyone in range could use your internet.")
            hotspot.apply(f"Internet sharing is {'on' if hotspot.internet else 'off'}.")
    elif command == "stop":
        hotspot.pause() if hotspot.enabled else say("The hotspot is already paused.")
    elif command == "start":
        say("The hotspot is already running.") if hotspot.enabled else hotspot.resume()
    elif command == "exit":
        return False
    else:
        say(f"Unknown command {parts[0]}. Type help for a list of commands.")
    return True


def read_command() -> str:
    global _at_prompt
    interactive = sys.stdin.isatty()
    _at_prompt = interactive
    try:
        return input(PROMPT if interactive else "")
    finally:
        _at_prompt = False


def command_loop(hotspot: Hotspot) -> None:
    while True:
        try:
            line = read_command()
        except EOFError:  # Without console input the hotspot keeps running until Ctrl+C.
            while True:
                time.sleep(1)
        if not execute(hotspot, line):
            return


def shut_down(hotspot: Hotspot, timeout: float = 20) -> None:
    global _at_prompt
    _at_prompt = False
    log("Stopping hotspot...")
    hotspot.close(timeout)
    log("The hotspot is off.")


_console_callback = None


def handle_console_events(hotspot: Hotspot) -> None:
    """Ctrl+C and closing the window shut the hotspot down cleanly and exit."""
    global _console_callback

    def handler(event: int) -> bool:
        say()
        shut_down(hotspot, 20 if event in (0, 1) else 4)  # Windows allows only a few seconds when the window closes.
        os._exit(0)

    _console_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)(handler)
    ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)  # The parent process may have disabled Ctrl+C.
    ctypes.windll.kernel32.SetConsoleCtrlHandler(_console_callback, True)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="pocketap", description="Wi-Fi hotspot that runs even without internet.")
    parser.add_argument("ssid", nargs="?", default=DEFAULT_SSID, help=f"network name (default {DEFAULT_SSID})")
    parser.add_argument("-p", "--password", default=DEFAULT_PASSWORD, help=f"password (default {DEFAULT_PASSWORD})")
    parser.add_argument("--internet", action="store_true", help=argparse.SUPPRESS)  # Survives the UAC relaunch.
    args = parser.parse_args()
    problem = ssid_problem(args.ssid) or password_problem(args.password)
    if problem:
        parser.error(problem)
    return args


def main() -> int:
    sys.stdout.reconfigure(errors="replace")
    args = parse_arguments()
    hotspot = Hotspot(args.ssid, args.password, args.internet)
    handle_console_events(hotspot)
    say("PocketAP - type help for a list of commands\n")
    log("Starting hotspot...")
    hotspot.thread.start()
    command_loop(hotspot)
    shut_down(hotspot)
    return 0


if __name__ == "__main__":
    sys.exit(main())
