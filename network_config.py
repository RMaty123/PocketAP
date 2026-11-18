"""Rozhodnutí o adresaci a bezpečná konfigurace virtuálního rozhraní AP.

Windows může rozhraní nakonfigurovat sám (typicky ICS: 192.168.137.1 a DHCP na
UDP 67). V takovém případě aplikace nic nemění a vlastní DHCP nespouští. Dva DHCP
servery v jedné síti by si navzájem konkurovaly.
"""
from __future__ import annotations

import ipaddress
import time
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Callable, Iterable, Sequence

from adapter_manager import AdapterError, AdapterInfo, Ipv4Entry, list_adapters, list_all_ipv4
from logger import get_logger
from runtime_state import RuntimeState
from settings import NetworkSettings
from system_utils import CommandError, CommandResult, is_admin, run_command, run_powershell_json

log = get_logger("network")

_DHCP_LISTENERS_PS = r"""
Get-NetUDPEndpoint -LocalPort 67 -ErrorAction SilentlyContinue | ForEach-Object {
  $owner = [int]$_.OwningProcess
  $process = Get-Process -Id $owner -ErrorAction SilentlyContinue
  $services = @(Get-CimInstance Win32_Service -Filter "ProcessId=$owner" -ErrorAction SilentlyContinue | ForEach-Object { $_.Name })
  [pscustomobject]@{ Address = $_.LocalAddress; Pid = $owner; Process = [string]$process.ProcessName; Services = $services }
}
"""


class NetworkConfigError(RuntimeError):
    """Virtuální rozhraní nelze bezpečně nakonfigurovat."""


class AddressingMode(Enum):
    """Kdo zajišťuje adresu hostitele a DHCP."""

    WINDOWS_MANAGED = "windows"
    OWN_DHCP = "own"


@dataclass(frozen=True)
class DhcpListener:
    """Proces poslouchající na UDP portu 67."""

    address: str
    pid: int
    process: str
    services: tuple[str, ...] = ()

    @property
    def description(self) -> str:
        if "SharedAccess" in self.services:
            return "Windows ICS (služba SharedAccess)"
        if self.services:
            return f"služba {', '.join(self.services)}"
        return f"proces {self.process or 'neznámý'} (PID {self.pid})"


@dataclass(frozen=True)
class AddressingPlan:
    """Výsledek detekce pro virtuální rozhraní."""

    mode: AddressingMode
    host_ip: str | None
    prefix: int | None
    listener: DhcpListener | None
    reason: str


def find_dhcp_listeners() -> list[DhcpListener]:
    """Zjistí, kdo v systému poslouchá na UDP 67."""
    try:
        items = run_powershell_json(_DHCP_LISTENERS_PS)
    except CommandError as exc:
        raise NetworkConfigError(f"Obsazení portu UDP 67 nelze zjistit: {exc}") from exc
    listeners: list[DhcpListener] = []
    for item in items:
        services = item.get("Services") or []
        if isinstance(services, str):
            services = [services]
        listeners.append(
            DhcpListener(
                address=str(item.get("Address") or ""),
                pid=int(item.get("Pid") or 0),
                process=str(item.get("Process") or ""),
                services=tuple(str(name) for name in services),
            )
        )
    return listeners


def decide_addressing(adapter: AdapterInfo, listeners: Iterable[DhcpListener]) -> AddressingPlan:
    """Rozhodne, zda DHCP zajišťuje Windows, nebo je potřeba vlastní server."""
    listener_by_address = {listener.address: listener for listener in listeners}
    for entry in adapter.usable_ipv4:
        listener = listener_by_address.get(entry.ip)
        if listener is not None:
            return AddressingPlan(
                mode=AddressingMode.WINDOWS_MANAGED,
                host_ip=entry.ip,
                prefix=entry.prefix,
                listener=listener,
                reason=f"Rozhraní má adresu {entry.ip}/{entry.prefix} a DHCP zajišťuje {listener.description}.",
            )
    if adapter.usable_ipv4:
        reason = "Rozhraní má adresu, ale žádný DHCP server na ní neposlouchá."
    else:
        reason = "Rozhraní nemá použitelnou adresu ani DHCP server."
    return AddressingPlan(AddressingMode.OWN_DHCP, None, None, None, reason)


def wait_for_addressing(
    if_index: int,
    timeout: float,
    should_abort: Callable[[], str | None] = lambda: None,
    poll_interval: float = 1.0,
) -> tuple[AddressingPlan, AdapterInfo]:
    """Dá Windows čas na konfiguraci rozhraní a vrátí výsledný plán."""
    deadline = time.monotonic() + timeout
    while True:
        reason = should_abort()
        if reason:
            raise NetworkConfigError(reason)
        adapter = next((a for a in list_adapters() if a.if_index == if_index), None)
        if adapter is None:
            raise NetworkConfigError("Virtuální rozhraní AP během detekce adresy zmizelo.")
        plan = decide_addressing(adapter, find_dhcp_listeners())
        if plan.mode is AddressingMode.WINDOWS_MANAGED or time.monotonic() >= deadline:
            return plan, adapter
        time.sleep(poll_interval)


def find_subnet_conflict(
    settings: NetworkSettings, if_index: int, addresses: Iterable[dict[str, Any]]
) -> str | None:
    """Vrátí popis rozhraní, které už používá překrývající se podsíť."""
    wanted = settings.network
    for entry in addresses:
        if int(entry.get("IfIndex") or 0) == if_index:
            continue
        try:
            other = ipaddress.IPv4Network(f"{entry['Ip']}/{entry['Prefix']}", strict=False)
        except (KeyError, ValueError):
            continue
        if other.prefixlen > 0 and wanted.overlaps(other):
            return f"{entry.get('Alias', 'jiné rozhraní')} ({entry['Ip']}/{entry['Prefix']})"
    return None


Runner = Callable[[Sequence[str]], CommandResult]


class NetworkConfigurator:
    """Mění výhradně virtuální adaptér Wi-Fi Direct a umí změnu vrátit."""

    def __init__(self, state: RuntimeState, runner: Runner = run_command) -> None:
        self._state = state
        self._runner = runner

    def _netsh(self, *args: str) -> CommandResult:
        try:
            return self._runner(["netsh", "interface", "ipv4", *args])
        except CommandError as exc:
            raise NetworkConfigError(str(exc)) from exc

    def apply_static(self, adapter: AdapterInfo, settings: NetworkSettings) -> None:
        """Nastaví adresu hostitele bez výchozí brány a DNS."""
        if not adapter.is_wifi_direct_virtual:
            raise NetworkConfigError(
                f"Odmítnuto: rozhraní '{adapter.name}' není virtuální adaptér Wi-Fi Direct."
            )
        if not is_admin():
            raise NetworkConfigError(
                "Windows pro toto rozhraní neposkytuje DHCP a nastavení vlastní adresy vyžaduje "
                "oprávnění správce. Spusťte PocketAP jako správce."
            )
        conflict = find_subnet_conflict(settings, adapter.if_index, list_all_ipv4())
        if conflict:
            raise NetworkConfigError(
                f"Podsíť {settings.network} už používá {conflict}. Změňte adresy v config.json."
            )

        # Záznam se ukládá před změnou, aby ji šlo vrátit i po pádu aplikace.
        self._state.set_network(
            {
                "if_index": adapter.if_index,
                "mac": adapter.mac,
                "host_ip": settings.host_ip,
                "dhcp_enabled": adapter.dhcp_enabled,
                "addresses": [asdict(entry) for entry in adapter.ipv4 if not entry.is_link_local],
            }
        )
        result = self._netsh(
            "set", "address", f"name={adapter.if_index}", "source=static",
            f"address={settings.host_ip}", f"mask={settings.netmask}", "gateway=none",
        )
        if not result.ok:
            self._state.set_network(None)
            raise NetworkConfigError(f"Adresu nelze nastavit: {result.output or result.returncode}")
        self._wait_for_address(adapter.if_index, settings.host_ip)
        log.info("Rozhraní '%s' má adresu %s/%d.", adapter.name, settings.host_ip, settings.prefix_length)

    @staticmethod
    def _wait_for_address(if_index: int, ip: str, timeout: float = 8.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            adapter = next((a for a in list_adapters() if a.if_index == if_index), None)
            if adapter and any(entry.ip == ip for entry in adapter.ipv4):
                return
            time.sleep(0.5)
        raise NetworkConfigError(f"Adresa {ip} se na rozhraní do {timeout:.0f} s neobjevila.")

    def restore(self) -> None:
        """Vrátí původní nastavení, pokud je aplikace změnila."""
        saved = self._state.network
        if not saved:
            return
        if_index = int(saved["if_index"])
        try:
            adapter = next((a for a in list_adapters() if a.if_index == if_index), None)
        except AdapterError as exc:
            raise NetworkConfigError(str(exc)) from exc
        if adapter is None or adapter.mac != saved.get("mac") or not adapter.is_wifi_direct_virtual:
            log.info("Virtuální rozhraní už neexistuje, není co obnovovat.")
            self._state.set_network(None)
            return

        if saved.get("dhcp_enabled"):
            result = self._netsh("set", "address", f"name={if_index}", "source=dhcp")
        else:
            result = self._netsh("delete", "address", f"name={if_index}", f"address={saved['host_ip']}")
            for raw in saved.get("addresses", []):
                entry = Ipv4Entry(str(raw["ip"]), int(raw["prefix"]))
                mask = ipaddress.IPv4Network(f"0.0.0.0/{entry.prefix}").netmask
                self._netsh("add", "address", f"name={if_index}", f"address={entry.ip}", f"mask={mask}")
        if not result.ok:
            raise NetworkConfigError(f"Původní adresu nelze obnovit: {result.output or result.returncode}")
        self._state.set_network(None)
        log.info("Původní konfigurace virtuálního rozhraní byla obnovena.")
