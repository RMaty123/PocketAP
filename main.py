"""Vstupní bod aplikace PocketAP."""
from __future__ import annotations

import argparse
import atexit
import ctypes
import sys
import threading
from ctypes import wintypes
from typing import Callable, Sequence

from bootstrap import ensure_runtime
from logger import get_logger, setup_logging
from settings import ConfigError, load_config
from system_utils import acquire_single_instance, is_admin, relaunch_elevated

log = get_logger("main")

_CTRL_C_EVENT = 0
_CTRL_BREAK_EVENT = 1
_HandlerRoutine = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
_console_handler: object | None = None  # Reference brání uvolnění callbacku.


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="PocketAP", description="Lokální Wi-Fi Access Point přes Wi-Fi Direct")
    parser.add_argument("--diagnose", action="store_true", help="ověří podporu Wi-Fi Direct a skončí")
    parser.add_argument("--no-live", action="store_true", help="diagnostika bez skutečného spuštění AP")
    parser.add_argument("--no-elevate", action="store_true", help="nevyžadovat oprávnění správce přes UAC")
    parser.add_argument("--verbose", action="store_true", help="podrobný výpis do konzole")
    return parser.parse_args(argv)


def install_console_handler(request_exit: Callable[[], None], shutdown: Callable[[], None]) -> None:
    """Ctrl+C ukončí aplikaci, zavření konzole stihne hotspot uklidit synchronně."""
    global _console_handler

    def handler(event: int) -> bool:
        request_exit()
        if event not in (_CTRL_C_EVENT, _CTRL_BREAK_EVENT):
            shutdown()
        return True

    _console_handler = _HandlerRoutine(handler)
    ctypes.windll.kernel32.SetConsoleCtrlHandler(_console_handler, True)


def run_gui(config: object) -> int:
    """Spustí okno aplikace a po jeho zavření vše uklidí."""
    import tkinter as tk

    from controller import HotspotController
    from gui import PocketApWindow

    records = setup_logging()
    controller = HotspotController(config)  # type: ignore[arg-type]
    for problem in controller.recover_stale_state():
        log.warning("%s", problem)

    exit_requested = threading.Event()
    shutdown = lambda: controller.shutdown(timeout=4.0)  # noqa: E731
    install_console_handler(exit_requested.set, shutdown)
    atexit.register(controller.shutdown)

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass

    root = tk.Tk()
    PocketApWindow(root, config, controller, records, exit_requested)  # type: ignore[arg-type]
    if not is_admin():
        log.warning("Aplikace běží bez oprávnění správce. Vlastní DHCP a pravidla firewallu nebudou dostupné.")
    root.mainloop()
    controller.shutdown()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = parse_arguments(arguments)
    # Musí proběhnout první: bez balíčků WinRT se aplikace spustí znovu ve složce .venv.
    exit_code = ensure_runtime(arguments)
    if exit_code is not None:
        return exit_code

    setup_logging(args.verbose)
    try:
        config = load_config()
    except ConfigError as exc:
        log.error("Chyba konfigurace: %s", exc)
        return 2

    if args.diagnose:
        if acquire_single_instance() is None:
            log.error("PocketAP už běží. Před diagnostikou jej ukončete.")
            return 3
        from diagnostics import run_diagnostics

        return 0 if run_diagnostics(config, live=not args.no_live) else 1

    if not is_admin() and not args.no_elevate:
        forwarded = ["--verbose"] if args.verbose else []
        if relaunch_elevated(forwarded):
            return 0
        log.warning("Výzva UAC byla odmítnuta, pokračuji s omezenými oprávněními.")

    if acquire_single_instance() is None:
        log.error("PocketAP už běží.")
        return 3
    return run_gui(config)


if __name__ == "__main__":
    sys.exit(main())
