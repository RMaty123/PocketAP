"""Zjišťování Wi-Fi adaptérů a virtuálního rozhraní vytvořeného pro AP.

Rozhraní se identifikuje porovnáním stavu před a po startu AP, nikoli podle
pevného názvu nebo čísla. Windows může použít libovolnou z instancí
„Microsoft Wi-Fi Direct Virtual Adapter #N“.
"""
from __future__ import annotations

import ipaddress
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

from logger import get_logger
from system_utils import CommandError, run_powershell_json

log = get_logger("adapter")

NDIS_MEDIUM_NATIVE_802_11 = 9
_WFD_DESCRIPTION = "wi-fi direct virtual adapter"
_WFD_COMPONENT_ID = "vwifimp_wfd"
_APIPA = ipaddress.ip_network("169.254.0.0/16")

_LIST_ADAPTERS_PS = r"""
$addresses = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue)
$interfaces = @(Get-NetIPInterface -AddressFamily IPv4 -ErrorAction SilentlyContinue)
Get-NetAdapter -IncludeHidden | Where-Object {
  $_.NdisPhysicalMedium -eq 9 -or $_.InterfaceDescription -like '*Wi-Fi Direct*' -or $_.ComponentID -like '*vwifimp_wfd*'
} | ForEach-Object {
  $index = [int]$_.ifIndex
  $ipv4 = @($addresses | Where-Object { $_.InterfaceIndex -eq $index } | ForEach-Object {
    [pscustomobject]@{ Ip = $_.IPAddress; Prefix = [int]$_.PrefixLength; Origin = [string]$_.PrefixOrigin }
  })
  $interface = $interfaces | Where-Object { $_.InterfaceIndex -eq $index } | Select-Object -First 1
  [pscustomobject]@{
    IfIndex = $index; Name = $_.Name; Description = $_.InterfaceDescription
    Status = [string]$_.Status; Mac = $_.MacAddress; ComponentId = [string]$_.ComponentID
    Medium = [int]$_.NdisPhysicalMedium; Virtual = [bool]$_.Virtual
    Dhcp = $(if ($interface) { [string]$interface.Dhcp } else { '' }); Ipv4 = $ipv4
  }
}
"""

_LIST_ALL_IPV4_PS = r"""
Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | ForEach-Object {
  [pscustomobject]@{ IfIndex = [int]$_.InterfaceIndex; Alias = $_.InterfaceAlias; Ip = $_.IPAddress; Prefix = [int]$_.PrefixLength }
}
"""


class AdapterError(RuntimeError):
    """Adaptér nebo virtuální rozhraní nelze zjistit."""


@dataclass(frozen=True)
class Ipv4Entry:
    """Jedna adresa IPv4 na rozhraní."""

    ip: str
    prefix: int
    origin: str = ""

    @property
    def is_link_local(self) -> bool:
        return ipaddress.ip_address(self.ip) in _APIPA


@dataclass(frozen=True)
class AdapterInfo:
    """Stav síťového adaptéru v okamžiku snímku."""

    if_index: int
    name: str
    description: str
    status: str
    mac: str
    component_id: str = ""
    medium: int = 0
    virtual: bool = False
    dhcp_enabled: bool | None = None
    ipv4: tuple[Ipv4Entry, ...] = ()

    @property
    def is_up(self) -> bool:
        return self.status.lower() == "up"

    @property
    def is_wifi_direct_virtual(self) -> bool:
        return (
            _WFD_DESCRIPTION in self.description.lower()
            or _WFD_COMPONENT_ID in self.component_id.lower()
        )

    @property
    def is_physical_wifi(self) -> bool:
        return (
            self.medium == NDIS_MEDIUM_NATIVE_802_11
            and not self.virtual
            and not self.is_wifi_direct_virtual
        )

    @property
    def usable_ipv4(self) -> tuple[Ipv4Entry, ...]:
        """Adresy kromě automatických APIPA 169.254.0.0/16."""
        return tuple(entry for entry in self.ipv4 if not entry.is_link_local)


@dataclass(frozen=True)
class Neighbor:
    """Záznam z tabulky ARP na rozhraní AP."""

    ip: str
    mac: str
    state: str


def _parse_adapter(item: dict[str, Any]) -> AdapterInfo:
    raw_ipv4 = item.get("Ipv4") or []
    if isinstance(raw_ipv4, dict):
        raw_ipv4 = [raw_ipv4]
    dhcp_text = str(item.get("Dhcp") or "").lower()
    dhcp: bool | None = None if not dhcp_text else dhcp_text == "enabled"
    return AdapterInfo(
        if_index=int(item["IfIndex"]),
        name=str(item.get("Name") or ""),
        description=str(item.get("Description") or ""),
        status=str(item.get("Status") or ""),
        mac=str(item.get("Mac") or ""),
        component_id=str(item.get("ComponentId") or ""),
        medium=int(item.get("Medium") or 0),
        virtual=bool(item.get("Virtual")),
        dhcp_enabled=dhcp,
        ipv4=tuple(
            Ipv4Entry(str(e["Ip"]), int(e["Prefix"]), str(e.get("Origin") or "")) for e in raw_ipv4
        ),
    )


def list_adapters() -> list[AdapterInfo]:
    """Vrátí fyzické Wi-Fi adaptéry a virtuální adaptéry Wi-Fi Direct."""
    try:
        return [_parse_adapter(item) for item in run_powershell_json(_LIST_ADAPTERS_PS)]
    except (CommandError, KeyError, TypeError, ValueError) as exc:
        raise AdapterError(f"Seznam síťových adaptérů nelze načíst: {exc}") from exc


def list_physical_wifi_adapters() -> list[AdapterInfo]:
    """Vrátí pouze fyzické Wi-Fi adaptéry."""
    return [adapter for adapter in list_adapters() if adapter.is_physical_wifi]


def list_all_ipv4() -> list[dict[str, Any]]:
    """Vrátí adresy IPv4 všech rozhraní (kontrola překryvu podsítí)."""
    try:
        return run_powershell_json(_LIST_ALL_IPV4_PS)
    except CommandError as exc:
        raise AdapterError(f"Adresy IPv4 nelze načíst: {exc}") from exc


def detect_new_virtual_interfaces(
    before: Iterable[AdapterInfo], after: Iterable[AdapterInfo]
) -> list[AdapterInfo]:
    """Najde virtuální rozhraní, která jsou nově vytvořená nebo nově aktivní."""
    previous = {adapter.if_index: adapter for adapter in before}
    found: list[AdapterInfo] = []
    for adapter in after:
        if not adapter.is_wifi_direct_virtual or not adapter.is_up:
            continue
        old = previous.get(adapter.if_index)
        if old is None or not old.is_up or old.mac != adapter.mac:
            found.append(adapter)
    return found


def wait_for_virtual_interface(
    before: Sequence[AdapterInfo],
    timeout: float,
    should_abort: Callable[[], str | None] = lambda: None,
    poll_interval: float = 0.5,
) -> AdapterInfo:
    """Počká, až se po startu AP objeví právě jedno nové virtuální rozhraní."""
    deadline = time.monotonic() + timeout
    candidates: list[AdapterInfo] = []
    while time.monotonic() < deadline:
        reason = should_abort()
        if reason:
            raise AdapterError(reason)
        candidates = detect_new_virtual_interfaces(before, list_adapters())
        if len(candidates) == 1:
            return candidates[0]
        addressed = [adapter for adapter in candidates if adapter.usable_ipv4]
        if len(addressed) == 1:
            return addressed[0]
        time.sleep(poll_interval)

    if candidates:
        names = ", ".join(f"{a.name} (ifIndex {a.if_index})" for a in candidates)
        raise AdapterError(f"Virtuální rozhraní AP nelze určit jednoznačně. Kandidáti: {names}.")
    raise AdapterError(
        f"AP se spustil, ale do {timeout:.0f} s se neaktivovalo žádné virtuální rozhraní "
        "Microsoft Wi-Fi Direct Virtual Adapter."
    )


def wait_for_interface_down(if_index: int, timeout: float, poll_interval: float = 0.5) -> bool:
    """Počká na deaktivaci rozhraní, aby další START správně rozpoznal změnu."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            current = next((a for a in list_adapters() if a.if_index == if_index), None)
        except AdapterError:
            current = None
        if current is None or not current.is_up:
            return True
        time.sleep(poll_interval)
    return False


def virtual_matches_physical(virtual_mac: str, physical_mac: str) -> bool:
    """Virtuální MAC se od fyzické obvykle liší jen v prvním oktetu."""
    return virtual_mac.replace(":", "-").upper().split("-")[1:] == physical_mac.replace(
        ":", "-"
    ).upper().split("-")[1:]


def filter_neighbors(
    entries: Iterable[dict[str, Any]], network: ipaddress.IPv4Network, host_ip: str
) -> list[Neighbor]:
    """Ponechá jen skutečné klienty v podsíti AP."""
    neighbors: list[Neighbor] = []
    for entry in entries:
        ip_text = str(entry.get("Ip") or "")
        mac = str(entry.get("Mac") or "").upper()
        state = str(entry.get("State") or "")
        try:
            ip = ipaddress.IPv4Address(ip_text)
        except ValueError:
            continue
        if ip not in network or ip_text == host_ip or ip == network.broadcast_address:
            continue
        if ip == network.network_address or not mac or mac in {"00-00-00-00-00-00", "FF-FF-FF-FF-FF-FF"}:
            continue
        if state.lower() not in {"reachable", "stale", "delay", "probe"}:
            continue
        neighbors.append(Neighbor(ip_text, mac, state))
    return neighbors


def get_neighbors(if_index: int, network: ipaddress.IPv4Network, host_ip: str) -> list[Neighbor]:
    """Přečte tabulku ARP rozhraní. Jde o odhad, záznamy mohou chvíli doznívat."""
    body = (
        f"Get-NetNeighbor -InterfaceIndex {int(if_index)} -AddressFamily IPv4 "
        "-ErrorAction SilentlyContinue | ForEach-Object { [pscustomobject]@{ "
        "Ip = $_.IPAddress; Mac = $_.LinkLayerAddress; State = [string]$_.State } }"
    )
    try:
        return filter_neighbors(run_powershell_json(body), network, host_ip)
    except CommandError as exc:
        log.debug("Tabulku ARP nelze přečíst: %s", exc)
        return []
