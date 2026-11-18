"""Diagnostický režim: ověří podporu Wi-Fi Direct včetně skutečného startu AP."""
from __future__ import annotations

import platform
import struct
import sys
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

import adapter_manager as adapters
from adapter_manager import AdapterError
from controller import HotspotController, HotspotState, StartRequest
from logger import LOG_DIR
from settings import AppConfig, generate_passphrase
from system_utils import CommandError, is_admin, run_powershell_json
from wifi_direct import WiFiDirectApError, count_wifi_direct_device_interfaces

Emit = Callable[[str], None]


class Outcome(Enum):
    PASS = "OK"
    WARN = "VAROVÁNÍ"
    FAIL = "CHYBA"
    INFO = "INFO"


@dataclass(frozen=True)
class CheckResult:
    name: str
    outcome: Outcome
    detail: str


def _check_platform() -> list[CheckResult]:
    build = sys.getwindowsversion().build
    results = [
        CheckResult(
            "Windows", Outcome.PASS if build >= 10240 else Outcome.FAIL,
            f"{platform.platform()} (build {build})",
        ),
    ]
    python_ok = sys.version_info >= (3, 11) and struct.calcsize("P") == 8
    results.append(
        CheckResult(
            "Python", Outcome.PASS if python_ok else Outcome.FAIL,
            f"{platform.python_version()} ({struct.calcsize('P') * 8}bit), vyžadováno 3.11+ 64bit",
        )
    )
    results.append(
        CheckResult(
            "Oprávnění", Outcome.PASS if is_admin() else Outcome.INFO,
            "správce" if is_admin() else "běžný uživatel (vlastní DHCP a firewall vyžadují správce)",
        )
    )
    return results


def _check_services() -> list[CheckResult]:
    body = (
        "Get-Service WlanSvc,SharedAccess,icssvc -ErrorAction SilentlyContinue | ForEach-Object { "
        "[pscustomobject]@{ Name = $_.Name; Status = [string]$_.Status } }"
    )
    try:
        states = {str(item["Name"]).lower(): str(item["Status"]) for item in run_powershell_json(body)}
    except CommandError as exc:
        return [CheckResult("Služby Windows", Outcome.WARN, str(exc))]
    wlan = states.get("wlansvc", "nenalezena")
    results = [
        CheckResult("Služba WLAN AutoConfig", Outcome.PASS if wlan == "Running" else Outcome.FAIL, wlan),
        CheckResult("Služba ICS (SharedAccess)", Outcome.INFO, states.get("sharedaccess", "nenalezena")),
    ]
    hotspot = states.get("icssvc", "nenalezena")
    results.append(
        CheckResult(
            "Mobile Hotspot (icssvc)", Outcome.WARN if hotspot == "Running" else Outcome.PASS,
            "běží, Mobile Hotspot má před Wi-Fi Direct přednost" if hotspot == "Running" else hotspot,
        )
    )
    return results


def _check_adapters() -> list[CheckResult]:
    try:
        found = adapters.list_adapters()
    except AdapterError as exc:
        return [CheckResult("Wi-Fi adaptéry", Outcome.FAIL, str(exc))]
    physical = [a for a in found if a.is_physical_wifi]
    virtual = [a for a in found if a.is_wifi_direct_virtual]
    results = [
        CheckResult(
            "Fyzický Wi-Fi adaptér", Outcome.PASS if physical else Outcome.FAIL,
            "; ".join(f"{a.description} [{a.status}]" for a in physical) or "nenalezen",
        ),
        CheckResult(
            "Virtuální adaptér Wi-Fi Direct", Outcome.PASS if virtual else Outcome.WARN,
            "; ".join(f"{a.description} [{a.status}]" for a in virtual)
            or "zatím neexistuje, Windows jej může vytvořit až při startu AP",
        ),
    ]
    return results


def _check_winrt() -> list[CheckResult]:
    try:
        count = count_wifi_direct_device_interfaces()
    except WiFiDirectApError as exc:
        return [CheckResult("WinRT Windows.Devices.WiFiDirect", Outcome.FAIL, str(exc))]
    return [
        CheckResult("WinRT Windows.Devices.WiFiDirect", Outcome.PASS, "API je dostupné"),
        CheckResult(
            "Rozhraní Wi-Fi Direct", Outcome.PASS if count else Outcome.WARN, f"počet nalezených: {count}"
        ),
    ]


def _live_test(config: AppConfig, emit: Emit) -> list[CheckResult]:
    """Skutečně spustí AP s náhodným heslem a znovu ho zastaví."""
    emit("Živý test: krátce spouštím testovací AP 'PocketAP-Diag'...")
    controller = HotspotController(config)
    finished = threading.Event()

    def on_snapshot(snapshot: object) -> None:
        if getattr(snapshot, "state", None) in (HotspotState.RUNNING, HotspotState.ERROR):
            finished.set()

    controller.set_listener(on_snapshot)
    limit = config.timeouts.ap_start_seconds + config.timeouts.interface_wait_seconds + config.timeouts.dhcp_detect_seconds + 10
    results: list[CheckResult] = []
    try:
        controller.start(StartRequest("PocketAP-Diag", generate_passphrase()))
        if not finished.wait(limit):
            return [CheckResult("Živý start AP", Outcome.FAIL, f"Nedokončeno do {limit:.0f} s.")]
        snapshot = controller.snapshot
        if snapshot.state is not HotspotState.RUNNING:
            return [CheckResult("Živý start AP", Outcome.FAIL, snapshot.message or "AP se nespustil.")]
        results.append(CheckResult("Živý start AP", Outcome.PASS, "Windows potvrdil stav Started"))
        results.append(CheckResult("Virtuální rozhraní", Outcome.PASS, snapshot.interface))
        results.append(CheckResult("Adresa hostitele", Outcome.PASS, f"{snapshot.host_ip}/{snapshot.prefix}"))
        results.append(CheckResult("Poskytovatel DHCP", Outcome.PASS, snapshot.dhcp_provider))
        time.sleep(2)
        controller.stop().result(config.timeouts.ap_stop_seconds * 2 + 10)
        stopped = controller.snapshot
        results.append(
            CheckResult(
                "Zastavení AP", Outcome.PASS if stopped.state is HotspotState.STOPPED else Outcome.FAIL,
                stopped.message or "AP i síťová konfigurace byly uklizeny",
            )
        )
        return results
    finally:
        controller.shutdown()


def run_diagnostics(config: AppConfig, live: bool = True, emit: Emit = print) -> bool:
    """Provede kontroly, uloží zprávu a vrátí True, pokud nic zásadního neselhalo."""
    emit("=== Diagnostika PocketAP ===")
    results = _check_platform() + _check_services() + _check_adapters() + _check_winrt()
    prerequisites_ok = not any(result.outcome is Outcome.FAIL for result in results)
    if live and prerequisites_ok:
        results += _live_test(config, emit)
    elif live:
        results.append(CheckResult("Živý start AP", Outcome.INFO, "přeskočeno kvůli chybám výše"))

    lines = [f"[{result.outcome.value:<8}] {result.name}: {result.detail}" for result in results]
    for line in lines:
        emit(line)
    success = not any(result.outcome is Outcome.FAIL for result in results)
    verdict = "Wi-Fi Direct Legacy AP je podporován." if success else "Byly nalezeny chyby, viz výpis výše."
    if not live and success:
        verdict = "Základní kontroly prošly, živý test AP nebyl spuštěn."
    emit(f"Výsledek: {verdict}")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    report: Path = LOG_DIR / f"diagnostika-{time.strftime('%Y%m%d-%H%M%S')}.txt"
    report.write_text("\n".join([*lines, f"Výsledek: {verdict}"]) + "\n", encoding="utf-8")
    emit(f"Zpráva uložena: {report}")
    return success
