"""Trvalý záznam provedených změn, aby šly vrátit i po pádu aplikace."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from logger import get_logger

STATE_PATH = Path(__file__).resolve().parent / "runtime_state.json"
log = get_logger("state")


class RuntimeState:
    """Eviduje vlastní pravidla firewallu a původní nastavení virtuálního rozhraní."""

    def __init__(self, path: Path = STATE_PATH) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {"firewall_rules": [], "network": None}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Záznam o změnách nelze načíst (%s), bude ignorován.", exc)
            return
        rules = raw.get("firewall_rules", [])
        self._data["firewall_rules"] = [str(rule) for rule in rules] if isinstance(rules, list) else []
        network = raw.get("network")
        self._data["network"] = network if isinstance(network, dict) else None

    def _save(self) -> None:
        if not self._data["firewall_rules"] and not self._data["network"]:
            self._path.unlink(missing_ok=True)
            return
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self._path)

    @property
    def firewall_rules(self) -> list[str]:
        with self._lock:
            return list(self._data["firewall_rules"])

    def add_firewall_rule(self, name: str) -> None:
        with self._lock:
            if name not in self._data["firewall_rules"]:
                self._data["firewall_rules"].append(name)
                self._save()

    def remove_firewall_rule(self, name: str) -> None:
        with self._lock:
            if name in self._data["firewall_rules"]:
                self._data["firewall_rules"].remove(name)
                self._save()

    @property
    def network(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._data["network"]) if self._data["network"] else None

    def set_network(self, value: dict[str, Any] | None) -> None:
        with self._lock:
            self._data["network"] = value
            self._save()
