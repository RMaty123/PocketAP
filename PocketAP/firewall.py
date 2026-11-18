"""Cílená pravidla Windows Firewallu. Firewall se nikdy nevypíná.

Aplikace maže jen pravidla, která sama vytvořila a eviduje v RuntimeState.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from logger import get_logger
from runtime_state import RuntimeState
from system_utils import CommandError, CommandResult, is_admin, real_python_executable, run_command

log = get_logger("firewall")

RULE_PREFIX = "PocketAP - "
DHCP_RULE_NAME = f"{RULE_PREFIX}DHCP server"
TEST_SERVER_RULE_NAME = f"{RULE_PREFIX}Test server"

Runner = Callable[[Sequence[str]], CommandResult]


class FirewallError(RuntimeError):
    """Pravidlo firewallu nelze vytvořit nebo odstranit."""


@dataclass(frozen=True)
class FirewallRule:
    """Příchozí povolení omezené na Wi-Fi rozhraní a interpret aplikace."""

    name: str
    protocol: str
    local_ports: str
    remote_ports: str = "any"
    local_ip: str = "any"
    remote_ip: str = "any"

    def add_arguments(self, program: str) -> list[str]:
        return [
            "netsh", "advfirewall", "firewall", "add", "rule",
            f"name={self.name}", "dir=in", "action=allow", "enable=yes", "profile=any",
            "interfacetype=wireless", f"program={program}", f"protocol={self.protocol}",
            f"localport={self.local_ports}", f"remoteport={self.remote_ports}",
            f"localip={self.local_ip}", f"remoteip={self.remote_ip}",
        ]


def dhcp_rule() -> FirewallRule:
    """DISCOVER chodí z 0.0.0.0 na broadcast, proto nejde omezit adresami."""
    return FirewallRule(DHCP_RULE_NAME, "UDP", "67", remote_ports="68")


def test_server_rule(host_ip: str, subnet: str, ports: Sequence[int]) -> FirewallRule:
    """Testovací server je dostupný jen z podsítě AP."""
    return FirewallRule(
        TEST_SERVER_RULE_NAME, "TCP", ",".join(str(port) for port in ports),
        local_ip=host_ip, remote_ip=subnet,
    )


class FirewallManager:
    """Vytváří a uklízí vlastní pravidla firewallu."""

    def __init__(self, state: RuntimeState, runner: Runner = run_command) -> None:
        self._state = state
        self._runner = runner

    def _run(self, args: Sequence[str]) -> CommandResult:
        try:
            return self._runner(args)
        except CommandError as exc:
            raise FirewallError(str(exc)) from exc

    def add_rule(self, rule: FirewallRule) -> None:
        """Vytvoří pravidlo a zaeviduje ho ještě před provedením změny."""
        if not rule.name.startswith(RULE_PREFIX):
            raise FirewallError(f"Název pravidla musí začínat '{RULE_PREFIX}'.")
        if not is_admin():
            raise FirewallError(f"Pravidlo '{rule.name}' vyžaduje oprávnění správce.")
        self._delete(rule.name)
        self._state.add_firewall_rule(rule.name)
        result = self._run(rule.add_arguments(real_python_executable()))
        if not result.ok:
            self._state.remove_firewall_rule(rule.name)
            raise FirewallError(f"Pravidlo '{rule.name}' nelze vytvořit: {result.output or result.returncode}")
        log.info("Přidáno pravidlo firewallu: %s", rule.name)

    def _delete(self, name: str) -> None:
        shown = self._run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"])
        if not shown.ok:  # Pravidlo neexistuje.
            return
        result = self._run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={name}"])
        if not result.ok:
            raise FirewallError(f"Pravidlo '{name}' nelze odstranit: {result.output or result.returncode}")

    def remove_all(self) -> list[str]:
        """Odstraní evidovaná pravidla a vrátí seznam případných chyb."""
        errors: list[str] = []
        for name in self._state.firewall_rules:
            if not name.startswith(RULE_PREFIX):
                self._state.remove_firewall_rule(name)
                continue
            try:
                self._delete(name)
                self._state.remove_firewall_rule(name)
                log.info("Odstraněno pravidlo firewallu: %s", name)
            except FirewallError as exc:
                errors.append(str(exc))
        return errors
