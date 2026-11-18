"""Pomocné funkce pro Windows: spouštění příkazů, oprávnění a UAC."""
from __future__ import annotations

import ctypes
import json
import subprocess
import sys
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

CREATE_NO_WINDOW = 0x08000000
ERROR_ALREADY_EXISTS = 183
SW_SHOWNORMAL = 1

_PS_PREAMBLE = (
    "$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';"
    "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false);"
)


class CommandError(RuntimeError):
    """Systémový příkaz se nepodařilo spustit nebo skončil chybou."""


@dataclass(frozen=True)
class CommandResult:
    """Výsledek externího příkazu."""

    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        return " ".join(part.strip() for part in (self.stdout, self.stderr) if part.strip())


def run_command(args: Sequence[str], timeout: float = 30.0, encoding: str = "oem") -> CommandResult:
    """Spustí příkaz bez okna konzole. Nástroj netsh vypisuje v kódování OEM."""
    try:
        completed = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            encoding=encoding,
            errors="replace",
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CommandError(f"Příkaz {args[0]} se nepodařilo dokončit: {exc}") from exc
    return CommandResult(tuple(args), completed.returncode, completed.stdout or "", completed.stderr or "")


def run_powershell_json(body: str, timeout: float = 30.0) -> list[dict[str, Any]]:
    """Spustí skript PowerShellu a vrátí jeho výstup jako seznam objektů."""
    script = f"{_PS_PREAMBLE}$r=@(& {{ {body} }});ConvertTo-Json -InputObject $r -Depth 5 -Compress"
    result = run_command(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        timeout=timeout,
        encoding="utf-8",
    )
    if not result.ok:
        raise CommandError(f"PowerShell skončil chybou: {result.output or result.returncode}")
    text = result.stdout.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CommandError(f"PowerShell vrátil neplatný JSON: {exc}") from exc
    return data if isinstance(data, list) else [data]


def is_admin() -> bool:
    """Zjistí, zda proces běží s oprávněním správce."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def relaunch_elevated(arguments: Sequence[str]) -> bool:
    """Znovu spustí aplikaci přes UAC. Vrací False, pokud uživatel výzvu odmítl."""
    script = str(Path(sys.argv[0]).resolve())
    parameters = subprocess.list2cmdline([script, *arguments])
    shell_execute = ctypes.windll.shell32.ShellExecuteW
    shell_execute.restype = wintypes.HINSTANCE
    handle = shell_execute(
        None, "runas", sys.executable, parameters, str(Path(script).parent), SW_SHOWNORMAL
    )
    return int(handle or 0) > 32


def acquire_single_instance(name: str = "Local\\PocketAP.SingleInstance") -> int | None:
    """Získá pojmenovaný mutex. Vrací None, pokud už aplikace běží."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle or ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        return None
    return int(handle)


def real_python_executable() -> str:
    """Vrátí skutečný interpret. Spouštěč ve venv totiž sokety sám neotevírá."""
    return str(getattr(sys, "_base_executable", None) or sys.executable)
