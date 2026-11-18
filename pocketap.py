"""PocketAP - Wi-Fi hotspot pro Windows, který běží i bez připojení k internetu.

Použití:  python pocketap.py [SSID] [-p HESLO]
Za běhu se ovládá příkazy v konzoli, seznam vypíše příkaz help.

Hotspot běží přes Wi-Fi Direct Legacy AP. Adresy klientům přiděluje Windows (ICS),
a když to neudělá, spustí se vlastní DHCP server (ten vyžaduje práva správce).
Sdílení internetu je ve výchozím stavu vypnuté, zapíná se příkazem internet zap.
Chybějící balíčky WinRT si aplikace při prvním spuštění sama nainstaluje.
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
    """Vypíše chybu a nechá okno otevřené, aby šla přečíst i po spuštění dvojklikem."""
    print(message, flush=True)
    try:
        input("Stiskni Enter pro zavření okna...")
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
    print("Instaluji potřebné balíčky, poprvé je potřeba internet...", flush=True)
    command = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *WINRT_PACKAGES]
    if subprocess.run(command, check=False).returncode != 0:
        exit_with_error("Balíčky se nepodařilo nainstalovat. Je potřeba internet a Python 3.11 nebo novější.")
    site.addsitedir(site.getusersitepackages())  # Složka mohla vzniknout až při této instalaci.
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
  if (-not $route) { throw 'Počítač teď není připojený k internetu.' }
  $upstream = Get-NetAdapter -IncludeHidden -InterfaceIndex $route.InterfaceIndex
  $guid = (Get-NetAdapter -IncludeHidden -InterfaceIndex $hotspot).InterfaceGuid
  $lan = $null
  foreach ($attempt in 1..20) {
    $lan = Get-SharingConfig | Where-Object { $_.Guid -eq $guid } | Select-Object -First 1
    if ($lan) { break }
    Start-Sleep -Milliseconds 500
  }
  $wan = Get-SharingConfig | Where-Object { $_.Guid -eq $upstream.InterfaceGuid } | Select-Object -First 1
  if (-not $lan -or -not $wan) { throw 'Windows u tohoto připojení sdílení nepodporuje.' }
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
  if (-not $ip) { throw 'Hotspot nedostal adresu od sdílení Windows.' }
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
    """Chyba s textem pro uživatele. Hotspot se po ní zkusí spustit znovu."""


class NeedsAdmin(Exception):
    """Windows neposkytuje DHCP a vlastní server vyžaduje práva správce."""


PROMPT = "pocketap> "
HELP = """Příkazy:
  status            zobrazí stav hotspotu
  ssid NÁZEV        změní název sítě
  heslo NOVÉ_HESLO  změní heslo (8 až 63 znaků bez diakritiky)
  internet zap|vyp  zapne nebo vypne sdílení internetu (výchozí je vyp)
  stop              pozastaví hotspot
  start             znovu spustí hotspot
  help              zobrazí tuto nápovědu
  exit              vypne hotspot a ukončí aplikaci

Změny platí jen do ukončení aplikace."""

_output_lock = threading.Lock()
_last_message = None
_at_prompt = False


def say(text: str = "") -> None:
    """Vypíše odpověď na příkaz."""
    with _output_lock:
        print(text, flush=True)


def log(text: str) -> None:
    """Vypíše událost s časem nad řádek s výzvou. Stejnou zprávu po sobě neopakuje."""
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
        raise HotspotError(f"Stav síťových adaptérů nelze zjistit: {exc}") from exc


def adapters() -> dict[int, dict]:
    """Aktivní virtuální adaptéry Wi-Fi Direct podle čísla rozhraní."""
    return {item["Index"]: item for item in powershell_json(ADAPTERS_PS)}


def is_admin() -> bool:
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def relaunch_as_admin(ssid: str, password: str, internet: bool) -> bool:
    """Spustí aplikaci znovu přes UAC. Vrací False, když uživatel výzvu odmítl."""
    script = Path(__file__).resolve()
    options = [f"--password={password}", *(["--internet"] if internet else [])]
    parameters = subprocess.list2cmdline([str(script), *options, "--", ssid])
    return ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, parameters, str(script.parent), 1) > 32


def quietly(action) -> None:
    """Úklid musí pokračovat, i když jeden krok selže."""
    try:
        action()
    except OSError:
        pass


class AccessPoint:
    """Běžící Wi-Fi Direct Legacy AP včetně příjmu klientů."""

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
                raise HotspotError("Windows do 25 s nepotvrdil spuštění hotspotu.")
            self.check()
            self.listener = wd.WiFiDirectConnectionListener()
            self.listener_token = self.listener.add_connection_requested(self._on_request)
        except OSError as exc:
            raise HotspotError(f"Wi-Fi Direct selhalo: {exc}") from exc

    def check(self) -> None:
        if self.ended.is_set():
            raise HotspotError(self.error or "Windows hotspot zastavil.")

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
            return "Wi-Fi Direct je obsazené. Vypni Mobilní hotspot nebo Miracast."
        return "Windows hotspot přerušil. Zkontroluj, že je zapnutá Wi-Fi a vypnutý režim Letadlo."

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
            address = pairs[0].remote_host_name.display_name if len(pairs) else "IP zatím neznámá"
            token = device.add_connection_status_changed(self._on_device_status)
            with self.lock:
                if self.stopping:
                    device.close()
                    return
                self.devices[device_id] = (device, token, name)
            log(f"+ Připojeno: {name} ({address})")
        except OSError as exc:
            log(f"Klienta {name} se nepodařilo přijmout: {exc}")
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
            log(f"- Odpojeno: {entry[2]}")

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
    """Malý DHCP server pro síť /24, který běží jen na rozhraní hotspotu."""

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
        """Vrátí současnou, požadovanou, nebo první volnou adresu."""
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
        """Sestaví odpověď a určí, kam se má poslat."""
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
        """Zpracuje zprávu klienta a vrátí odpověď, pokud nějaká patří."""
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
        for attempt in range(16):  # Nová adresa rozhraní chvíli naskakuje.
            try:
                sock.bind((self.host, 67))
                break
            except OSError as exc:
                if attempt == 15:
                    sock.close()
                    raise HotspotError(f"DHCP server nelze spustit na {self.host}: {exc}") from exc
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
                continue  # Vadný paket ani chyba soketu server neukončí.

    def stop(self) -> None:
        self.stopped.set()
        if self.thread is not None:
            self.thread.join(timeout=3)
        if self.sock is not None:
            self.sock.close()


def free_host_address(if_index: int) -> str:
    """Najde podsíť, kterou nepoužívá žádné jiné rozhraní počítače."""
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
    raise HotspotError("Nenašla se volná podsíť pro hotspot.")


def start_own_dhcp(if_index: int) -> DhcpServer:
    if not is_admin():
        raise NeedsAdmin
    host = free_host_address(if_index)
    result = run(
        "netsh", "interface", "ipv4", "set", "address", f"name={if_index}", "source=static",
        f"address={host}", "mask=255.255.255.0", "gateway=none",
    )
    if result.returncode != 0:
        raise HotspotError(f"Adresu {host} nelze nastavit: {result.stdout.strip()}")
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
    """Zapne sdílení internetu do hotspotu a vrátí adresu počítače v síti a název sdíleného připojení."""
    script = f"$hotspot = {if_index}\n{SHARING_PS}\n{START_SHARING_PS}"
    result = powershell_json(script, timeout=60)
    info = result[0] if result else {}
    if not info.get("Ip"):
        raise HotspotError(info.get("Error") or "Windows sdílení nezapnul.")
    return info["Ip"], info["Upstream"]


def stop_sharing() -> None:
    """Úklid po hotspotu nesmí selhat, případná chyba se jen vypíše."""
    try:
        result = powershell_json(f"{SHARING_PS}\n{STOP_SHARING_PS}", timeout=60)
    except HotspotError as exc:
        result = [{"Error": str(exc)}]
    if result:
        log(f"Sdílení internetu se nepodařilo vypnout: {result[0].get('Error')}")


def wait_for_adapter(before: dict[int, str], ap: AccessPoint) -> int:
    """Najde virtuální adaptér, který vznikl spuštěním hotspotu."""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        ap.check()
        for index, adapter in adapters().items():
            if before.get(index) != adapter["Mac"]:
                return index
        time.sleep(0.5)
    raise HotspotError("Windows nevytvořil virtuální adaptér hotspotu.")


def wait_for_windows_dhcp(if_index: int, ap: AccessPoint, stop: threading.Event) -> str | None:
    """Počká, jestli DHCP zajistí Windows, a vrátí adresu počítače v síti."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not stop.is_set():
        ap.check()
        adapter = adapters().get(if_index)
        if adapter is None:
            raise HotspotError("Virtuální adaptér hotspotu zmizel.")
        if adapter["Dhcp"]:
            return adapter["Dhcp"][0]
        time.sleep(1)
    return None


def watch(ap: AccessPoint, if_index: int, stop: threading.Event) -> None:
    """Hlídá hotspot až do ukončení. Při výpadku vyvolá chybu, která spustí restart."""
    seconds = missing = 0
    while not stop.wait(1):
        ap.check()
        seconds += 1
        if seconds % 5:
            continue
        try:
            missing = 0 if if_index in adapters() else missing + 1
        except HotspotError:
            continue
        if missing >= 2:
            raise HotspotError("Virtuální adaptér hotspotu se vypnul.")


class Hotspot:
    """Řídí hotspot na pozadí. Název sítě a heslo drží jen v paměti procesu."""

    def __init__(self, ssid: str, password: str, internet: bool = False) -> None:
        self.ssid = ssid
        self.password = password
        self.internet = internet
        self.upstream: str | None = None
        self.enabled = True
        self.closing = False
        self.state = "spouští se"
        self.host: str | None = None
        self.ap: AccessPoint | None = None
        self.changed = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def resume(self) -> None:
        self.enabled = True
        log("Spouštím hotspot...")
        self.changed.set()

    def pause(self) -> None:
        self.enabled = False
        log("Pozastavuji hotspot...")
        self.changed.set()

    def apply(self, message: str) -> None:
        """Běžící hotspot se s novým nastavením restartuje."""
        if self.enabled:
            log(f"{message} Restartuji hotspot...")
            self.changed.set()
        else:
            say(f"{message} Projeví se po příkazu start.")

    def close(self, timeout: float = 20) -> None:
        self.closing = True
        self.changed.set()
        self.thread.join(timeout)

    def internet_state(self) -> str:
        if self.upstream:
            return f"sdílí se z připojení {self.upstream}"
        return "zapnuto, zatím se nesdílí" if self.internet else "vypnuto"

    def describe(self) -> str:
        ap = self.ap
        return (
            f"  Stav:     {self.state}\n  Síť:      {self.ssid}\n  Heslo:    {self.password}\n"
            f"  IP:       {self.host or '-'}\n  Klienti:  {len(ap.devices) if ap else 0}\n"
            f"  Internet: {self.internet_state()}"
        )

    def _loop(self) -> None:
        while True:
            self.changed.clear()
            if self.closing:
                return
            if not self.enabled:
                self.state = "pozastaven"
                log("Hotspot je pozastaven. Znovu ho spustíš příkazem start.")
                self.changed.wait()
                continue
            self.state = "spouští se"
            try:
                self._run()
            except NeedsAdmin:
                self._request_admin()
            except Exception as exc:  # noqa: BLE001 - hotspot se má vždy pokusit znovu naběhnout
                self.state = "chyba, zkouší se znovu"
                log(f"{exc} Zkouším to znovu...")
                self.changed.wait(RETRY_SECONDS)

    def _request_admin(self) -> None:
        if self.internet:
            log("Sdílení internetu potřebuje práva správce.")
        else:
            log("Windows neposkytuje DHCP, vlastní server potřebuje práva správce.")
        if relaunch_as_admin(self.ssid, self.password, self.internet):
            os._exit(0)
        if self.internet:
            self.internet = False
            log("Bez práv správce se internet sdílet nedá, hotspot poběží bez něj.")
            return
        log("Bez práv správce nedostanou klienti IP adresu. Spusť aplikaci jako správce.")
        self.enabled = False

    def _share_internet(self, if_index: int) -> str | None:
        """Selhání sdílení hotspot neshodí, poběží dál jako místní síť."""
        try:
            host, self.upstream = start_sharing(if_index)
            return host
        except HotspotError as exc:
            log(f"Internet se nepodařilo sdílet: {exc} Hotspot poběží bez něj.")
            return None

    def _run(self) -> None:
        """Jeden běh hotspotu. Skončí po změně požadavku, nebo chybou."""
        ssid, password, internet = self.ssid, self.password, self.internet
        if internet and not is_admin():
            raise NeedsAdmin
        before = {index: adapter["Mac"] for index, adapter in adapters().items()}
        ap = self.ap = AccessPoint()
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
            self.state = "běží"
            shared = f", internet z připojení {self.upstream}" if self.upstream else ""
            log(f"Hotspot běží: síť {ssid}, heslo {password}, IP {host}{shared}")
            watch(ap, if_index, self.changed)
        finally:
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
        """Další start pozná nový adaptér jen tehdy, když ten starý už není aktivní."""
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
        return "Název sítě musí mít 1 až 32 bajtů."
    return None


def password_problem(password: str) -> str | None:
    if not (8 <= len(password) <= 63 and password.isascii() and password.isprintable()):
        return "Heslo musí mít 8 až 63 znaků bez diakritiky."
    return None


def execute(hotspot: Hotspot, line: str) -> bool:
    """Provede příkaz z konzole. Vrací False, když se má aplikace ukončit."""
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
            say(f"{problem} Použití: ssid NÁZEV")
        elif value == hotspot.ssid:
            say("Síť už se takto jmenuje.")
        else:
            hotspot.ssid = value
            hotspot.apply(f"Název sítě je teď {value}.")
    elif command == "heslo":
        problem = password_problem(value)
        if problem:
            say(f"{problem} Použití: heslo NOVÉ_HESLO")
        elif value == hotspot.password:
            say("Tohle heslo už je nastavené.")
        else:
            hotspot.password = value
            hotspot.apply("Heslo je změněné.")
    elif command == "internet":
        choice = value.lower()
        if choice not in ("zap", "vyp"):
            say(f"Sdílení internetu: {hotspot.internet_state()}. Použití: internet zap|vyp")
        elif (choice == "zap") == hotspot.internet:
            say(f"Sdílení internetu už je {'zapnuté' if hotspot.internet else 'vypnuté'}.")
        else:
            hotspot.internet = choice == "zap"
            if hotspot.internet and hotspot.password == DEFAULT_PASSWORD:
                say("Pozor, síť má výchozí heslo a internet by mohl používat kdokoli v dosahu.")
            hotspot.apply(f"Sdílení internetu je {'zapnuté' if hotspot.internet else 'vypnuté'}.")
    elif command == "stop":
        hotspot.pause() if hotspot.enabled else say("Hotspot už je pozastavený.")
    elif command == "start":
        say("Hotspot už běží.") if hotspot.enabled else hotspot.resume()
    elif command == "exit":
        return False
    else:
        say(f"Neznámý příkaz {parts[0]}. Seznam příkazů vypíše help.")
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
        except EOFError:  # Bez vstupu z konzole běží hotspot dál až do Ctrl+C.
            while True:
                time.sleep(1)
        if not execute(hotspot, line):
            return


def shut_down(hotspot: Hotspot, timeout: float = 20) -> None:
    global _at_prompt
    _at_prompt = False
    log("Vypínám hotspot...")
    hotspot.close(timeout)
    log("Hotspot je vypnutý.")


_console_callback = None


def handle_console_events(hotspot: Hotspot) -> None:
    """Ctrl+C i zavření okna hotspot korektně vypnou a ukončí aplikaci."""
    global _console_callback

    def handler(event: int) -> bool:
        say()
        shut_down(hotspot, 20 if event in (0, 1) else 4)  # Při zavření okna dá Windows jen pár sekund.
        os._exit(0)

    _console_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)(handler)
    ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)  # Ctrl+C mohl vypnout rodičovský proces.
    ctypes.windll.kernel32.SetConsoleCtrlHandler(_console_callback, True)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="pocketap", description="Wi-Fi hotspot, který běží i bez internetu.")
    parser.add_argument("ssid", nargs="?", default=DEFAULT_SSID, help=f"název sítě (výchozí {DEFAULT_SSID})")
    parser.add_argument("-p", "--password", default=DEFAULT_PASSWORD, help=f"heslo (výchozí {DEFAULT_PASSWORD})")
    parser.add_argument("--internet", action="store_true", help=argparse.SUPPRESS)  # Přežije restart přes UAC.
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
    say("PocketAP - seznam příkazů vypíše help\n")
    log("Spouštím hotspot...")
    hotspot.thread.start()
    command_loop(hotspot)
    shut_down(hotspot)
    return 0


if __name__ == "__main__":
    sys.exit(main())
