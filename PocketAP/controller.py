"""Řízení životního cyklu hotspotu: START, běh, STOP a úklid po chybě."""
from __future__ import annotations

import ipaddress
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from enum import Enum
from typing import Callable

import adapter_manager as adapters
from adapter_manager import AdapterError, AdapterInfo, Neighbor
from dhcp_server import LEASES_PATH, DhcpServer, DhcpServerError, LeasePool
from firewall import FirewallError, FirewallManager, dhcp_rule, test_server_rule
from logger import get_logger
from network_config import AddressingMode, AddressingPlan, NetworkConfigError, NetworkConfigurator, wait_for_addressing
from runtime_state import RuntimeState
from settings import AppConfig, ConfigError, validate_passphrase, validate_ssid
from system_utils import CommandError, is_admin
from test_server import LocalTestServer
from wifi_direct import WiFiDirectAccessPoint, WiFiDirectApError

log = get_logger("controller")

_MONITOR_INTERVAL_SECONDS = 5.0
_EXPECTED_ERRORS = (
    AdapterError, CommandError, ConfigError, DhcpServerError, FirewallError,
    NetworkConfigError, WiFiDirectApError, OSError,
)


class HotspotState(str, Enum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    ERROR = "ERROR"


@dataclass(frozen=True)
class StartRequest:
    """Parametry zadané uživatelem."""

    ssid: str
    passphrase: str
    physical_mac: str | None = None
    test_server: bool = False


@dataclass(frozen=True)
class HotspotSnapshot:
    """Neměnný obraz stavu pro GUI a diagnostiku."""

    state: HotspotState = HotspotState.STOPPED
    message: str = ""
    ssid: str = ""
    interface: str = ""
    host_ip: str = ""
    prefix: int | None = None
    dhcp_provider: str = ""
    dhcp_range: str = ""
    clients: int | None = None
    clients_source: str = ""
    test_url: str = ""


class HotspotController:
    """Všechny změny stavu běží v jednom pracovním vlákně, takže se nepřekrývají."""

    def __init__(self, config: AppConfig, state: RuntimeState | None = None) -> None:
        self._config = config
        self._state = state or RuntimeState()
        self._firewall = FirewallManager(self._state)
        self._network = NetworkConfigurator(self._state)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pocketap-control")
        self._lock = threading.Lock()
        self._snapshot = HotspotSnapshot()
        self._listener: Callable[[HotspotSnapshot], None] | None = None
        self._ap: WiFiDirectAccessPoint | None = None
        self._dhcp: DhcpServer | None = None
        self._test_server: LocalTestServer | None = None
        self._interface: AdapterInfo | None = None
        self._neighbors: list[Neighbor] = []
        self._monitor_stop = threading.Event()
        self._monitor: threading.Thread | None = None
        self._shutdown = False

    @property
    def snapshot(self) -> HotspotSnapshot:
        with self._lock:
            return self._snapshot

    def set_listener(self, listener: Callable[[HotspotSnapshot], None] | None) -> None:
        self._listener = listener

    def _publish(self, **changes: object) -> None:
        with self._lock:
            self._snapshot = replace(self._snapshot, **changes)  # type: ignore[arg-type]
            snapshot = self._snapshot
        if self._listener:
            self._listener(snapshot)

    def _reset(self, state: HotspotState, message: str = "") -> None:
        with self._lock:
            self._snapshot = HotspotSnapshot(state=state, message=message)
            snapshot = self._snapshot
        if self._listener:
            self._listener(snapshot)

    def recover_stale_state(self) -> list[str]:
        """Vrátí změny, které po sobě zanechal případný pád aplikace."""
        if not self._state.firewall_rules and not self._state.network:
            return []
        if not is_admin():
            return ["Po minulém běhu zůstaly změny, jejich úklid vyžaduje spuštění jako správce."]
        problems = self._firewall.remove_all()
        try:
            self._network.restore()
        except NetworkConfigError as exc:
            problems.append(str(exc))
        if not problems:
            log.info("Změny z předchozího běhu byly vráceny.")
        return problems

    def start(self, request: StartRequest) -> Future:
        return self._executor.submit(self._do_start, request)

    def stop(self) -> Future:
        return self._executor.submit(self._do_stop)

    def shutdown(self, timeout: float = 20.0) -> None:
        """Bezpečně ukončí hotspot při zavření aplikace. Lze volat opakovaně."""
        if self._shutdown:
            return
        self._shutdown = True
        try:
            self._executor.submit(self._do_stop).result(timeout)
        except Exception as exc:  # noqa: BLE001 - ukončení musí doběhnout vždy
            log.error("Ukončení hotspotu nebylo dokončeno: %s", exc)
        self._executor.shutdown(wait=False)

    def _abort_reason(self) -> str | None:
        return self._ap.failure if self._ap else None

    def _do_start(self, request: StartRequest) -> None:
        if self.snapshot.state in (HotspotState.RUNNING, HotspotState.STARTING):
            return
        self._reset(HotspotState.STARTING)
        try:
            self._start_sequence(request)
        except _EXPECTED_ERRORS as exc:
            self._fail_start(str(exc))
        except Exception as exc:  # noqa: BLE001 - neúspěch se nesmí tvářit jako běžící AP
            log.exception("Neočekávaná chyba při startu.")
            self._fail_start(f"Neočekávaná chyba: {exc}")

    def _fail_start(self, message: str) -> None:
        log.error("Start hotspotu selhal: %s", message)
        problems = self._teardown()
        details = f"{message} Úklid: {'; '.join(problems)}" if problems else message
        self._reset(HotspotState.ERROR, details)

    def _start_sequence(self, request: StartRequest) -> None:
        ssid = validate_ssid(request.ssid)
        passphrase = validate_passphrase(request.passphrase)
        timeouts = self._config.timeouts
        self._publish(ssid=ssid)

        log.info("Spouštím Wi-Fi Direct Legacy AP...")
        before = adapters.list_adapters()
        self._ap = WiFiDirectAccessPoint(self._on_ap_aborted, self._refresh_clients)
        self._ap.start(ssid, passphrase, timeouts.ap_start_seconds)

        interface = adapters.wait_for_virtual_interface(
            before, timeouts.interface_wait_seconds, self._abort_reason
        )
        self._interface = interface
        log.info("Virtuální rozhraní: %s (ifIndex %d, %s)", interface.name, interface.if_index, interface.description)
        self._warn_if_other_adapter(interface, request.physical_mac)
        self._publish(interface=interface.name)

        plan, interface = wait_for_addressing(
            interface.if_index, timeouts.dhcp_detect_seconds, self._abort_reason
        )
        self._interface = interface
        log.info(plan.reason)
        host_ip, prefix = self._configure_addressing(plan, interface)

        if request.test_server:
            self._start_test_server(host_ip, prefix)
        self._start_monitor(interface, host_ip, prefix)
        self._refresh_clients()
        self._publish(state=HotspotState.RUNNING, message="")
        log.info("Hotspot běží. SSID '%s', adresa hostitele %s/%d.", ssid, host_ip, prefix)

    @staticmethod
    def _warn_if_other_adapter(interface: AdapterInfo, physical_mac: str | None) -> None:
        """WinRT neumožňuje zvolit adaptér, volbu lze jen ověřit podle MAC adresy."""
        if physical_mac and not adapters.virtual_matches_physical(interface.mac, physical_mac):
            log.warning(
                "Windows použil pro AP pravděpodobně jiný adaptér, než je vybraný. "
                "Wi-Fi Direct API výběr adaptéru neumožňuje."
            )

    def _configure_addressing(self, plan: AddressingPlan, interface: AdapterInfo) -> tuple[str, int]:
        if plan.mode is AddressingMode.WINDOWS_MANAGED:
            assert plan.host_ip is not None and plan.prefix is not None and plan.listener is not None
            network = ipaddress.IPv4Network(f"{plan.host_ip}/{plan.prefix}", strict=False)
            log.info("Vlastní DHCP se nespouští, aby v síti nebyly dva DHCP servery.")
            self._publish(
                host_ip=plan.host_ip, prefix=plan.prefix,
                dhcp_provider=plan.listener.description,
                dhcp_range=f"{network} (přiděluje Windows)",
            )
            return plan.host_ip, plan.prefix

        net = self._config.network
        self._network.apply_static(interface, net)
        self._firewall.add_rule(dhcp_rule())
        pool = LeasePool(
            net.dhcp_start, net.dhcp_end, net.lease_seconds,
            excluded={net.host_ip}, storage=LEASES_PATH,
        )
        self._dhcp = DhcpServer(
            net.host_ip, net.netmask, pool, if_index=interface.if_index,
            advertise_gateway=self._config.dhcp.advertise_host_as_gateway,
            conflict_timeout_ms=self._config.dhcp.conflict_probe_timeout_ms,
            on_change=self._refresh_clients,
        )
        self._dhcp.start()
        self._publish(
            host_ip=net.host_ip, prefix=net.prefix_length, dhcp_provider="PocketAP (vlastní DHCP server)",
            dhcp_range=f"{net.dhcp_start} – {net.dhcp_end}",
        )
        return net.host_ip, net.prefix_length

    def _start_test_server(self, host_ip: str, prefix: int) -> None:
        settings = self._config.test_server
        subnet = str(ipaddress.IPv4Network(f"{host_ip}/{prefix}", strict=False))
        try:
            self._firewall.add_rule(
                test_server_rule(host_ip, subnet, (settings.http_port, settings.echo_port))
            )
        except FirewallError as exc:
            log.warning("%s Klienti se k testovacímu serveru nemusí připojit.", exc)
        server = LocalTestServer(host_ip, settings.http_port, settings.echo_port)
        server.start()
        self._test_server = server
        self._publish(test_url=server.http_url)

    def _start_monitor(self, interface: AdapterInfo, host_ip: str, prefix: int) -> None:
        self._monitor_stop.clear()
        network = ipaddress.IPv4Network(f"{host_ip}/{prefix}", strict=False)
        self._monitor = threading.Thread(
            target=self._monitor_loop, args=(interface.if_index, network, host_ip),
            name="pocketap-monitor", daemon=True,
        )
        self._monitor.start()

    def _monitor_loop(self, if_index: int, network: ipaddress.IPv4Network, host_ip: str) -> None:
        missing = 0
        while not self._monitor_stop.wait(_MONITOR_INTERVAL_SECONDS):
            try:
                current = next((a for a in adapters.list_adapters() if a.if_index == if_index), None)
            except AdapterError as exc:
                log.debug("Kontrola rozhraní: %s", exc)
                continue
            missing = 0 if current and current.is_up else missing + 1
            if missing >= 2:
                self._submit_abort("Virtuální rozhraní AP se neočekávaně deaktivovalo.")
                return
            self._update_neighbors(adapters.get_neighbors(if_index, network, host_ip))

    def _update_neighbors(self, neighbors: list[Neighbor]) -> None:
        previous = {entry.ip for entry in self._neighbors}
        current = {entry.ip for entry in neighbors}
        for entry in neighbors:
            if entry.ip not in previous:
                log.info("Zařízení v síti: %s (MAC %s)", entry.ip, entry.mac)
        for ip in sorted(previous - current):
            log.info("Zařízení opustilo síť: %s", ip)
        self._neighbors = neighbors
        self._refresh_clients()

    def _refresh_clients(self) -> None:
        """Počet klientů uvádí vždy i zdroj, protože přesnost zdrojů se liší."""
        api_count = len(self._ap.clients) if self._ap else 0
        lease_count = len(self._dhcp.pool.active()) if self._dhcp else 0
        neighbor_count = len(self._neighbors)
        if api_count:
            self._publish(clients=api_count, clients_source="Wi-Fi Direct API")
        elif lease_count:
            self._publish(clients=lease_count, clients_source="DHCP zápůjčky")
        elif neighbor_count:
            self._publish(clients=neighbor_count, clients_source="odhad z tabulky ARP")
        else:
            self._publish(clients=0, clients_source="")

    def _on_ap_aborted(self, message: str) -> None:
        self._submit_abort(message)

    def _submit_abort(self, message: str) -> None:
        try:
            self._executor.submit(self._handle_abort, message)
        except RuntimeError:  # Aplikace se právě ukončuje.
            pass

    def _handle_abort(self, message: str) -> None:
        if self.snapshot.state is not HotspotState.RUNNING:
            return
        log.error("Hotspot byl přerušen: %s", message)
        problems = self._teardown()
        details = f"{message} Úklid: {'; '.join(problems)}" if problems else message
        self._reset(HotspotState.ERROR, details)

    def _do_stop(self) -> None:
        if self.snapshot.state in (HotspotState.STOPPED, HotspotState.ERROR) and self._ap is None:
            return
        self._publish(state=HotspotState.STOPPING, message="")
        log.info("Zastavuji hotspot...")
        problems = self._teardown()
        if problems:
            self._reset(HotspotState.ERROR, "Zastavení proběhlo s chybami: " + "; ".join(problems))
        else:
            self._reset(HotspotState.STOPPED)
            log.info("Hotspot je zastaven.")

    def _teardown(self) -> list[str]:
        """Uklidí vše v opačném pořadí. Chyba jednoho kroku nezastaví ostatní."""
        problems: list[str] = []

        def attempt(label: str, action: Callable[[], None]) -> None:
            try:
                action()
            except Exception as exc:  # noqa: BLE001 - úklid musí pokračovat
                log.error("%s: %s", label, exc)
                problems.append(f"{label}: {exc}")

        self._monitor_stop.set()
        if self._monitor is not None:
            self._monitor.join(timeout=3)
            self._monitor = None
        self._neighbors = []

        if self._test_server is not None:
            attempt("Testovací server", self._test_server.stop)
            self._test_server = None
        if self._dhcp is not None:
            attempt("DHCP server", self._dhcp.stop)
            self._dhcp = None
        problems.extend(self._firewall.remove_all())
        attempt("Obnovení sítě", self._network.restore)

        interface = self._interface
        if self._ap is not None:
            attempt("Wi-Fi Direct", lambda: self._ap.stop(self._config.timeouts.ap_stop_seconds))  # type: ignore[union-attr]
            self._ap = None
        if interface is not None:
            if not adapters.wait_for_interface_down(interface.if_index, self._config.timeouts.ap_stop_seconds):
                log.warning("Virtuální rozhraní zůstalo po zastavení AP aktivní.")
            self._interface = None
        return problems
