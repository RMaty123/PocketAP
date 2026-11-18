"""Příprava běhového prostředí, aby aplikace fungovala při libovolném způsobu spuštění.

Pokud aktuální interpret nemá balíčky WinRT, vytvoří se složka .venv, nainstalují
se do ní závislosti a aplikace se v ní spustí znovu. Modul používá jen standardní
knihovnu, protože běží ještě před instalací závislostí.
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
from pathlib import Path
from typing import Sequence

PROJECT_DIR = Path(__file__).resolve().parent
VENV_DIR = PROJECT_DIR / ".venv"
REQUIREMENTS = PROJECT_DIR / "requirements.txt"
MAIN_SCRIPT = PROJECT_DIR / "main.py"
MINIMUM_PYTHON = (3, 11)
EXIT_BOOTSTRAP_FAILED = 4

_CREATE_NO_WINDOW = 0x08000000
_MB_ICONERROR = 0x10
_IMPORT_CHECK = (
    "import winrt.windows.devices.wifidirect, winrt.windows.devices.enumeration, "
    "winrt.windows.security.credentials, winrt.windows.networking, "
    "winrt.windows.foundation, winrt.windows.foundation.collections, "
    "winrt.windows.storage.streams"
)


class BootstrapError(RuntimeError):
    """Běhové prostředí se nepodařilo připravit."""


def _run(args: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BootstrapError(f"Příkaz {Path(args[0]).name} se nepodařilo dokončit: {exc}") from exc


def _has_packages(python: Path | str) -> bool:
    try:
        return _run([str(python), "-c", _IMPORT_CHECK], timeout=60).returncode == 0
    except BootstrapError:
        return False


def _is_working(python: Path) -> bool:
    if not python.exists():
        return False
    try:
        return _run([str(python), "-c", "pass"], timeout=30).returncode == 0
    except BootstrapError:
        return False


def _status(message: str) -> None:
    if sys.stdout is not None:
        print(f"[PocketAP] {message}", flush=True)


def _last_lines(result: subprocess.CompletedProcess[str], count: int = 6) -> str:
    lines = (result.stderr or result.stdout or "").strip().splitlines()
    return "\n".join(lines[-count:]) or f"návratový kód {result.returncode}"


def _prepare_virtualenv() -> Path:
    """Vytvoří .venv a nainstaluje závislosti. Vrací cestu k jeho interpretu."""
    if sys.version_info < MINIMUM_PYTHON:
        wanted = ".".join(str(part) for part in MINIMUM_PYTHON)
        raise BootstrapError(f"PocketAP vyžaduje Python {wanted} nebo novější, nalezen {sys.version.split()[0]}.")

    python = VENV_DIR / "Scripts" / "python.exe"
    if not _is_working(python):
        _status("Vytvářím virtuální prostředí...")
        command = [sys.executable, "-m", "venv", str(VENV_DIR)]
        if VENV_DIR.exists():
            command.insert(3, "--clear")
        result = _run(command, timeout=180)
        if result.returncode != 0 or not _is_working(python):
            raise BootstrapError(f"Virtuální prostředí se nepodařilo vytvořit:\n{_last_lines(result)}")

    if not _has_packages(python):
        _status("Instaluji závislosti, může to chvíli trvat...")
        result = _run(
            [str(python), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(REQUIREMENTS)],
            timeout=600,
        )
        if result.returncode != 0:
            raise BootstrapError(
                f"Závislosti se nepodařilo nainstalovat. Zkontrolujte připojení k internetu.\n{_last_lines(result)}"
            )
        if not _has_packages(python):
            raise BootstrapError("Závislosti jsou nainstalované, ale balíčky WinRT nejdou načíst.")
    return python


def _launch(python: Path, arguments: Sequence[str]) -> int:
    """Spustí aplikaci ve virtuálním prostředí a počká na její ukončení."""
    interpreter = python
    if Path(sys.executable).stem.lower() == "pythonw":
        interpreter = python.with_name("pythonw.exe")
    process = subprocess.Popen([str(interpreter), str(MAIN_SCRIPT), *arguments], cwd=PROJECT_DIR)
    while True:
        try:
            return process.wait()
        except KeyboardInterrupt:  # Ctrl+C zpracuje spuštěná aplikace a korektně se ukončí.
            continue


def _show_error(message: str) -> None:
    if sys.stderr is not None:
        print(f"[PocketAP] CHYBA: {message}", file=sys.stderr, flush=True)
    ctypes.windll.user32.MessageBoxW(None, message, "PocketAP", _MB_ICONERROR)


def ensure_runtime(arguments: Sequence[str]) -> int | None:
    """Vrací None, když může aplikace pokračovat, jinak její návratový kód."""
    if _has_packages(sys.executable):
        return None
    try:
        return _launch(_prepare_virtualenv(), arguments)
    except (BootstrapError, OSError) as exc:
        _show_error(str(exc))
        return EXIT_BOOTSTRAP_FAILED
