"""Načtení a validace konfigurace (config.json, SSID, heslo, adresní plán)."""
from __future__ import annotations

import ipaddress
import json
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

# Bez znaků, které se na telefonu snadno spletou (0/O, 1/l/I).
_PASSWORD_ALPHABET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class ConfigError(ValueError):
    """Neplatná hodnota konfigurace nebo uživatelského vstupu."""


def validate_ssid(ssid: str) -> str:
    """Ověří SSID: 1 až 32 bajtů v UTF-8 bez řídicích znaků."""
    if not ssid or not ssid.strip():
        raise ConfigError("SSID nesmí být prázdné.")
    if len(ssid.encode("utf-8")) > 32:
        raise ConfigError("SSID může mít nejvýše 32 bajtů (znaky s diakritikou zabírají 2 bajty).")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in ssid):
        raise ConfigError("SSID nesmí obsahovat řídicí znaky.")
    return ssid


def validate_passphrase(passphrase: str) -> str:
    """Ověří heslo WPA2-PSK: 8 až 63 tisknutelných znaků ASCII."""
    if len(passphrase) < 8:
        raise ConfigError("Heslo WPA2 musí mít alespoň 8 znaků.")
    if len(passphrase) > 63:
        raise ConfigError("Heslo WPA2 může mít nejvýše 63 znaků.")
    if any(not 32 <= ord(ch) <= 126 for ch in passphrase):
        raise ConfigError("Heslo WPA2 smí obsahovat jen znaky ASCII (bez diakritiky).")
    return passphrase


def generate_passphrase(length: int = 16) -> str:
    """Vygeneruje kryptograficky náhodné heslo WPA2."""
    if not 8 <= length <= 63:
        raise ConfigError("Délka hesla musí být 8 až 63 znaků.")
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))


@dataclass(frozen=True)
class NetworkSettings:
    """Adresní plán pro režim s vlastním DHCP serverem."""

    host_ip: str = "192.168.50.1"
    netmask: str = "255.255.255.0"
    dhcp_start: str = "192.168.50.100"
    dhcp_end: str = "192.168.50.200"
    lease_seconds: int = 12 * 3600

    @property
    def network(self) -> ipaddress.IPv4Network:
        return ipaddress.IPv4Network(f"{self.host_ip}/{self.netmask}", strict=False)

    @property
    def prefix_length(self) -> int:
        return self.network.prefixlen

    @property
    def broadcast(self) -> str:
        return str(self.network.broadcast_address)

    def validate(self) -> None:
        """Ověří, že brána i rozsah DHCP leží ve stejné podsíti a nepřekrývají se."""
        try:
            host = ipaddress.IPv4Address(self.host_ip)
            start = ipaddress.IPv4Address(self.dhcp_start)
            end = ipaddress.IPv4Address(self.dhcp_end)
            network = self.network
        except ValueError as exc:
            raise ConfigError(f"Neplatná IPv4 konfigurace: {exc}") from exc

        reserved = {network.network_address, network.broadcast_address}
        if host not in network or host in reserved:
            raise ConfigError("Adresa hostitele není platná adresa v zadané podsíti.")
        if start not in network or end not in network:
            raise ConfigError("Rozsah DHCP musí ležet v podsíti hostitele.")
        if start > end:
            raise ConfigError("Začátek rozsahu DHCP je za jeho koncem.")
        if start in reserved or end in reserved:
            raise ConfigError("Rozsah DHCP nesmí obsahovat adresu sítě ani broadcast.")
        if start <= host <= end:
            raise ConfigError("Adresa hostitele nesmí ležet v rozsahu DHCP.")
        if self.lease_seconds < 60:
            raise ConfigError("Doba zápůjčky DHCP musí být alespoň 1 minuta.")


@dataclass(frozen=True)
class DhcpSettings:
    """Volby vlastního DHCP serveru."""

    # Ve výchozím stavu se brána ani DNS neposílají (izolovaná síť).
    advertise_host_as_gateway: bool = False
    conflict_probe_timeout_ms: int = 800


@dataclass(frozen=True)
class TimeoutSettings:
    """Časové limity jednotlivých fází startu a zastavení."""

    ap_start_seconds: float = 25
    interface_wait_seconds: float = 20
    dhcp_detect_seconds: float = 10
    ap_stop_seconds: float = 12


@dataclass(frozen=True)
class LocalServerSettings:
    """Porty testovacího HTTP a TCP echo serveru."""

    http_port: int = 8080
    echo_port: int = 9000


@dataclass(frozen=True)
class AppConfig:
    """Úplná konfigurace aplikace."""

    ssid: str = "PocketAP"
    passphrase: str = "PocketAP12345"
    network: NetworkSettings = field(default_factory=NetworkSettings)
    dhcp: DhcpSettings = field(default_factory=DhcpSettings)
    timeouts: TimeoutSettings = field(default_factory=TimeoutSettings)
    test_server: LocalServerSettings = field(default_factory=LocalServerSettings)


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"Sekce '{name}' v config.json musí být objekt.")
    return value


def parse_config(raw: dict[str, Any]) -> AppConfig:
    """Převede načtený JSON na ověřenou konfiguraci."""
    defaults = AppConfig()
    net = _section(raw, "network")
    dhcp = _section(raw, "dhcp")
    timeouts = _section(raw, "timeouts")
    server = _section(raw, "test_server")
    try:
        config = AppConfig(
            ssid=validate_ssid(str(raw.get("ssid", defaults.ssid))),
            passphrase=validate_passphrase(str(raw.get("passphrase", defaults.passphrase))),
            network=NetworkSettings(
                host_ip=str(net.get("host_ip", defaults.network.host_ip)),
                netmask=str(net.get("netmask", defaults.network.netmask)),
                dhcp_start=str(net.get("dhcp_range_start", defaults.network.dhcp_start)),
                dhcp_end=str(net.get("dhcp_range_end", defaults.network.dhcp_end)),
                lease_seconds=int(float(net.get("lease_time_hours", 12)) * 3600),
            ),
            dhcp=DhcpSettings(
                advertise_host_as_gateway=bool(dhcp.get("advertise_host_as_gateway", False)),
                conflict_probe_timeout_ms=int(dhcp.get("conflict_probe_timeout_ms", 800)),
            ),
            timeouts=TimeoutSettings(
                ap_start_seconds=float(timeouts.get("ap_start_seconds", 25)),
                interface_wait_seconds=float(timeouts.get("interface_wait_seconds", 20)),
                dhcp_detect_seconds=float(timeouts.get("dhcp_detect_seconds", 10)),
                ap_stop_seconds=float(timeouts.get("ap_stop_seconds", 12)),
            ),
            test_server=LocalServerSettings(
                http_port=int(server.get("http_port", 8080)),
                echo_port=int(server.get("echo_port", 9000)),
            ),
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError(f"Neplatná hodnota v config.json: {exc}") from exc

    config.network.validate()
    for port in (config.test_server.http_port, config.test_server.echo_port):
        if not 1 <= port <= 65535:
            raise ConfigError(f"Neplatný port testovacího serveru: {port}")
    return config


def load_config(path: Path = CONFIG_PATH) -> AppConfig:
    """Načte config.json. Pokud soubor chybí, použije výchozí hodnoty."""
    if not path.exists():
        return AppConfig()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Soubor {path.name} není platný JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"Soubor {path.name} musí obsahovat objekt JSON.")
    return parse_config(raw)
