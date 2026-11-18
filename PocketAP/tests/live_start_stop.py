"""Živý test opakovaného START/STOP na skutečném Wi-Fi adaptéru.

Spuštění ze složky PocketAP:
    python tests\\live_start_stop.py --cycles 3
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapter_manager import list_adapters  # noqa: E402
from controller import HotspotController, HotspotSnapshot, HotspotState, StartRequest  # noqa: E402
from logger import setup_logging  # noqa: E402
from network_config import find_dhcp_listeners  # noqa: E402
from settings import generate_passphrase, load_config  # noqa: E402


def check_local_services(host_ip: str, http_port: int, echo_port: int) -> None:
    """Ověří testovací servery z hostitele přes adresu AP."""
    with urllib.request.urlopen(f"http://{host_ip}:{http_port}/ping", timeout=5) as response:
        payload = json.load(response)
    assert payload["status"] == "ok", payload
    with socket.create_connection((host_ip, echo_port), timeout=5) as connection:
        connection.sendall(b"pocketap-echo")
        assert connection.recv(64) == b"pocketap-echo"


def run_cycle(controller: HotspotController, config, number: int) -> None:
    reached = threading.Event()

    def listener(snapshot: HotspotSnapshot) -> None:
        if snapshot.state in (HotspotState.RUNNING, HotspotState.ERROR):
            reached.set()

    controller.set_listener(listener)
    started = time.monotonic()
    controller.start(StartRequest(f"PocketAP-Live{number}", generate_passphrase(), test_server=True))
    assert reached.wait(90), "AP se nespustil v časovém limitu"
    snapshot = controller.snapshot
    assert snapshot.state is HotspotState.RUNNING, snapshot.message
    print(
        f"Cyklus {number}: RUNNING za {time.monotonic() - started:.1f} s, rozhraní '{snapshot.interface}', "
        f"{snapshot.host_ip}/{snapshot.prefix}, DHCP: {snapshot.dhcp_provider}"
    )
    check_local_services(snapshot.host_ip, config.test_server.http_port, config.test_server.echo_port)
    print(f"Cyklus {number}: HTTP i TCP echo odpovídají na {snapshot.host_ip}")

    controller.stop().result(60)
    assert controller.snapshot.state is HotspotState.STOPPED, controller.snapshot.message
    active = [a.name for a in list_adapters() if a.is_wifi_direct_virtual and a.is_up]
    assert not active, f"Virtuální rozhraní zůstalo aktivní: {active}"
    leftover = [l for l in find_dhcp_listeners() if l.address == snapshot.host_ip]
    assert not leftover, f"DHCP stále poslouchá: {leftover}"
    with socket.socket() as probe:  # Port testovacího serveru musí být uvolněný.
        probe.bind(("127.0.0.1", config.test_server.http_port))
    print(f"Cyklus {number}: STOPPED, rozhraní i porty jsou uvolněné")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycles", type=int, default=3)
    args = parser.parse_args()
    setup_logging()
    config = load_config()
    controller = HotspotController(config)
    try:
        for number in range(1, args.cycles + 1):
            run_cycle(controller, config, number)
    finally:
        controller.shutdown()
    print(f"VÝSLEDEK: {args.cycles} cykly START/STOP proběhly úspěšně.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
