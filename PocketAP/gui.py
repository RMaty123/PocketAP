"""Grafické rozhraní PocketAP v Tkinteru."""
from __future__ import annotations

import logging
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable

from adapter_manager import AdapterError, AdapterInfo, list_physical_wifi_adapters
from controller import HotspotController, HotspotSnapshot, HotspotState, StartRequest
from diagnostics import run_diagnostics
from logger import get_logger
from settings import AppConfig, ConfigError, generate_passphrase, validate_passphrase, validate_ssid

log = get_logger("gui")

BACKGROUND = "#eef2f7"
CARD = "#ffffff"
BORDER = "#d8e0ea"
INK = "#172033"
MUTED = "#5f6f84"
HEADER = "#111c2e"
GREEN = "#15803d"
GREEN_ACTIVE = "#116631"
RED = "#b91c1c"
RED_ACTIVE = "#971616"
FONT = "Segoe UI"

STATE_COLORS: dict[HotspotState, str] = {
    HotspotState.STOPPED: "#64748b",
    HotspotState.STARTING: "#b45309",
    HotspotState.RUNNING: GREEN,
    HotspotState.STOPPING: "#b45309",
    HotspotState.ERROR: RED,
}
LOG_COLORS = {"INFO": "#26364d", "WARNING": "#a16207", "ERROR": RED, "CRITICAL": RED}


class PocketApWindow:
    """Hlavní okno. Práce na pozadí předává výsledky přes frontu do vlákna Tk."""

    def __init__(
        self,
        root: tk.Tk,
        config: AppConfig,
        controller: HotspotController,
        log_records: "queue.Queue[logging.LogRecord]",
        exit_requested: threading.Event,
    ) -> None:
        self.root = root
        self.config = config
        self.controller = controller
        self.log_records = log_records
        self.exit_requested = exit_requested
        self.ui_queue: "queue.Queue[Callable[[], None]]" = queue.Queue()
        self.closed = threading.Event()
        self.closing = False
        self.diagnostics_running = False
        self.adapter_macs: dict[str, str] = {}
        self.state = HotspotState.STOPPED

        self.ssid_var = tk.StringVar(value=config.ssid)
        self.password_var = tk.StringVar(value=config.passphrase)
        self.show_password_var = tk.BooleanVar(value=False)
        self.adapter_var = tk.StringVar()
        self.test_server_var = tk.BooleanVar(value=False)
        self.info_vars: dict[str, tk.StringVar] = {}

        self._configure_window()
        self._build_header()
        self._build_body()
        self._apply_snapshot(controller.snapshot)
        controller.set_listener(lambda snapshot: self.ui_queue.put(lambda: self._apply_snapshot(snapshot)))
        threading.Thread(target=self._load_adapters, name="pocketap-adapters", daemon=True).start()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(100, self._poll)

    def _configure_window(self) -> None:
        self.root.title("PocketAP")
        self.root.geometry("960x780")
        self.root.minsize(860, 700)
        self.root.configure(bg=BACKGROUND)
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", font=(FONT, 10), background=CARD, foreground=INK)
        style.configure("TEntry", fieldbackground="#ffffff", bordercolor=BORDER, padding=6)
        style.configure("TCombobox", fieldbackground="#ffffff", bordercolor=BORDER, padding=5)
        style.map("TCombobox", fieldbackground=[("readonly", "#ffffff")])
        style.configure("TCheckbutton", background=CARD, foreground=INK)
        style.map("TCheckbutton", background=[("active", CARD)])
        style.configure("Secondary.TButton", padding=(10, 6), background="#e7edf5", bordercolor=BORDER)
        style.map("Secondary.TButton", background=[("active", "#d9e3ef")])
        for name, color, active in (("Start", GREEN, GREEN_ACTIVE), ("Stop", RED, RED_ACTIVE)):
            style.configure(
                f"{name}.TButton", font=(FONT, 11, "bold"), foreground="#ffffff", background=color,
                bordercolor=color, padding=(12, 11),
            )
            style.map(
                f"{name}.TButton",
                background=[("disabled", "#c5ceda"), ("active", active)],
                foreground=[("disabled", "#f6f8fb")],
                bordercolor=[("disabled", "#c5ceda")],
            )

    def _build_header(self) -> None:
        header = tk.Frame(self.root, bg=HEADER, padx=22, pady=14)
        header.pack(fill="x")
        titles = tk.Frame(header, bg=HEADER)
        titles.pack(side="left")
        tk.Label(titles, text="PocketAP", bg=HEADER, fg="#ffffff", font=(FONT, 20, "bold")).pack(anchor="w")
        tk.Label(
            titles, text="Lokální Wi-Fi Access Point bez internetu", bg=HEADER, fg="#b7c4d6", font=(FONT, 10)
        ).pack(anchor="w")
        self.state_pill = tk.Label(header, text="STOPPED", fg="#ffffff", font=(FONT, 11, "bold"), padx=16, pady=6)
        self.state_pill.pack(side="right")
        self.banner = tk.Label(
            self.root, bg="#fdecec", fg="#8f1414", font=(FONT, 10), anchor="w", justify="left",
            padx=22, pady=9, wraplength=900,
        )

    @staticmethod
    def _card(parent: tk.Misc, title: str) -> tk.Frame:
        outer = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        tk.Label(outer, text=title, bg=CARD, fg=INK, font=(FONT, 11, "bold")).pack(anchor="w", padx=16, pady=(12, 6))
        inner = tk.Frame(outer, bg=CARD)
        inner.pack(fill="both", expand=True, padx=16, pady=(0, 14))
        outer.inner = inner  # type: ignore[attr-defined]
        return outer

    def _build_body(self) -> None:
        self.body = tk.Frame(self.root, bg=BACKGROUND, padx=16, pady=14)
        self.body.pack(fill="both", expand=True)
        self.body.columnconfigure(0, weight=5, uniform="cards")
        self.body.columnconfigure(1, weight=4, uniform="cards")
        self.body.rowconfigure(1, weight=1)

        settings = self._card(self.body, "Nastavení hotspotu")
        settings.grid(row=0, column=0, sticky="nsew", padx=(0, 7), pady=(0, 12))
        self._build_settings(settings.inner)  # type: ignore[attr-defined]

        status = self._card(self.body, "Stav sítě")
        status.grid(row=0, column=1, sticky="nsew", padx=(7, 0), pady=(0, 12))
        self._build_status(status.inner)  # type: ignore[attr-defined]

        events = self._card(self.body, "Log událostí")
        events.grid(row=1, column=0, columnspan=2, sticky="nsew")
        self._build_log(events.inner)  # type: ignore[attr-defined]

    def _build_settings(self, parent: tk.Frame) -> None:
        parent.columnconfigure(1, weight=1)
        self._field_label(parent, "SSID", 0)
        self.ssid_entry = ttk.Entry(parent, textvariable=self.ssid_var)
        self.ssid_entry.grid(row=0, column=1, columnspan=2, sticky="ew", pady=4)

        self._field_label(parent, "Heslo WPA2", 1)
        self.password_entry = ttk.Entry(parent, textvariable=self.password_var, show="•")
        self.password_entry.grid(row=1, column=1, sticky="ew", pady=4)
        self.generate_button = ttk.Button(
            parent, text="Vygenerovat", style="Secondary.TButton", command=self._generate_password
        )
        self.generate_button.grid(row=1, column=2, padx=(8, 0), pady=4)
        ttk.Checkbutton(
            parent, text="Zobrazit heslo", variable=self.show_password_var, command=self._toggle_password
        ).grid(row=2, column=1, columnspan=2, sticky="w")

        self._field_label(parent, "Wi-Fi adaptér", 3)
        self.adapter_box = ttk.Combobox(parent, textvariable=self.adapter_var, state="readonly")
        self.adapter_box.grid(row=3, column=1, columnspan=2, sticky="ew", pady=(8, 2))
        tk.Label(
            parent, text="Adaptér pro AP vybírá Windows, volba slouží ke kontrole.",
            bg=CARD, fg=MUTED, font=(FONT, 9),
        ).grid(row=4, column=1, columnspan=2, sticky="w")

        self.test_server_check = ttk.Checkbutton(
            parent,
            text=f"Testovací server (HTTP {self.config.test_server.http_port}, TCP echo {self.config.test_server.echo_port})",
            variable=self.test_server_var,
        )
        self.test_server_check.grid(row=5, column=1, columnspan=2, sticky="w", pady=(6, 8))

        actions = tk.Frame(parent, bg=CARD)
        actions.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(4, 8))
        actions.columnconfigure((0, 1), weight=1, uniform="actions")
        self.start_button = ttk.Button(actions, text="START HOTSPOT", style="Start.TButton", command=self._start)
        self.start_button.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self.stop_button = ttk.Button(actions, text="STOP HOTSPOT", style="Stop.TButton", command=self._stop)
        self.stop_button.grid(row=0, column=1, sticky="ew", padx=(5, 0))
        self.diagnostics_button = ttk.Button(
            parent, text="Spustit diagnostiku Wi-Fi Direct", style="Secondary.TButton", command=self._diagnose
        )
        self.diagnostics_button.grid(row=7, column=0, columnspan=3, sticky="ew")

    @staticmethod
    def _field_label(parent: tk.Frame, text: str, row: int) -> None:
        tk.Label(parent, text=text, bg=CARD, fg=MUTED, font=(FONT, 10)).grid(
            row=row, column=0, sticky="w", padx=(0, 12)
        )

    def _build_status(self, parent: tk.Frame) -> None:
        parent.columnconfigure(1, weight=1)
        rows = (
            ("state", "Stav AP"), ("ssid", "SSID"), ("interface", "Rozhraní"), ("ip", "IP adresa"),
            ("provider", "DHCP zajišťuje"), ("range", "DHCP rozsah"), ("clients", "Klienti"), ("test", "Test server"),
        )
        for index, (key, title) in enumerate(rows):
            variable = tk.StringVar(value="–")
            self.info_vars[key] = variable
            tk.Label(parent, text=title, bg=CARD, fg=MUTED, font=(FONT, 10)).grid(
                row=index, column=0, sticky="nw", padx=(0, 12), pady=4
            )
            tk.Label(
                parent, textvariable=variable, bg=CARD, fg=INK, font=(FONT, 10, "bold"),
                anchor="w", justify="left", wraplength=250,
            ).grid(row=index, column=1, sticky="ew", pady=4)

    def _build_log(self, parent: tk.Frame) -> None:
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)
        self.log_text = tk.Text(
            parent, height=8, wrap="word", state="disabled", bg="#f8fafc", fg=INK, relief="flat",
            font=("Consolas", 9), padx=8, pady=6, highlightthickness=1, highlightbackground=BORDER,
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(parent, command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)
        for level, color in LOG_COLORS.items():
            self.log_text.tag_configure(level, foreground=color)

    def _load_adapters(self) -> None:
        try:
            found = list_physical_wifi_adapters()
        except AdapterError as exc:
            log.error("%s", exc)
            found = []
        self.ui_queue.put(lambda: self._show_adapters(found))

    def _show_adapters(self, found: list[AdapterInfo]) -> None:
        self.adapter_macs = {f"{a.description} ({a.name})": a.mac for a in found}
        labels = list(self.adapter_macs)
        self.adapter_box.configure(values=labels)
        if labels:
            self.adapter_var.set(labels[0])
        else:
            self.adapter_var.set("Nebyl nalezen žádný Wi-Fi adaptér")

    def _toggle_password(self) -> None:
        self.password_entry.configure(show="" if self.show_password_var.get() else "•")

    def _generate_password(self) -> None:
        self.password_var.set(generate_passphrase())
        self.show_password_var.set(True)
        self._toggle_password()

    def _show_banner(self, message: str) -> None:
        if message:
            self.banner.configure(text=message)
            self.banner.pack(fill="x", before=self.body)
        else:
            self.banner.pack_forget()

    def _start(self) -> None:
        try:
            ssid = validate_ssid(self.ssid_var.get())
            passphrase = validate_passphrase(self.password_var.get())
        except ConfigError as exc:
            self._show_banner(str(exc))
            return
        self._show_banner("")
        self.controller.start(
            StartRequest(ssid, passphrase, self.adapter_macs.get(self.adapter_var.get()), self.test_server_var.get())
        )

    def _stop(self) -> None:
        self.controller.stop()

    def _diagnose(self) -> None:
        self.diagnostics_running = True
        self._update_controls()
        threading.Thread(target=self._run_diagnostics, name="pocketap-diagnostics", daemon=True).start()

    def _run_diagnostics(self) -> None:
        try:
            success = run_diagnostics(self.config, live=True, emit=log.info)
            message = "" if success else "Diagnostika našla chyby, podrobnosti jsou v logu událostí."
        except Exception as exc:  # noqa: BLE001 - chyba se má ukázat v okně
            log.exception("Diagnostika selhala.")
            message = f"Diagnostika selhala: {exc}"
        self.ui_queue.put(lambda: self._finish_diagnostics(message))

    def _finish_diagnostics(self, message: str) -> None:
        self.diagnostics_running = False
        self._show_banner(message)
        self._update_controls()

    def _apply_snapshot(self, snapshot: HotspotSnapshot) -> None:
        self.state = snapshot.state
        self.state_pill.configure(text=snapshot.state.value, bg=STATE_COLORS[snapshot.state])
        network = self.config.network
        clients = "–" if snapshot.clients is None else str(snapshot.clients)
        if snapshot.clients_source:
            clients += f" ({snapshot.clients_source})"
        values: dict[str, str] = {
            "state": snapshot.state.value,
            "ssid": snapshot.ssid or "–",
            "interface": snapshot.interface or "–",
            "ip": f"{snapshot.host_ip}/{snapshot.prefix}" if snapshot.host_ip else "–",
            "provider": snapshot.dhcp_provider or "–",
            "range": snapshot.dhcp_range or f"{network.dhcp_start} – {network.dhcp_end} (vlastní DHCP)",
            "clients": clients,
            "test": snapshot.test_url or "–",
        }
        for key, value in values.items():
            self.info_vars[key].set(value)
        if snapshot.state is HotspotState.ERROR:
            self._show_banner(snapshot.message)
        elif snapshot.state in (HotspotState.STARTING, HotspotState.RUNNING):
            self._show_banner("")
        self._update_controls()

    def _update_controls(self) -> None:
        idle = self.state in (HotspotState.STOPPED, HotspotState.ERROR)
        can_edit = idle and not self.diagnostics_running and not self.closing
        self.start_button.state(["!disabled"] if can_edit else ["disabled"])
        running = self.state is HotspotState.RUNNING and not self.closing
        self.stop_button.state(["!disabled"] if running else ["disabled"])
        for widget in (self.ssid_entry, self.password_entry, self.generate_button, self.test_server_check, self.diagnostics_button):
            widget.state(["!disabled"] if can_edit else ["disabled"])
        self.adapter_box.configure(state="readonly" if can_edit else "disabled")

    def _drain_logs(self) -> None:
        lines: list[tuple[str, str]] = []
        while True:
            try:
                record = self.log_records.get_nowait()
            except queue.Empty:
                break
            stamp = time.strftime("%H:%M:%S", time.localtime(record.created))
            lines.append((f"{stamp}  {record.getMessage()}\n", record.levelname))
        if not lines:
            return
        self.log_text.configure(state="normal")
        for text, level in lines:
            self.log_text.insert("end", text, level)
        excess = int(self.log_text.index("end-1c").split(".")[0]) - 1500
        if excess > 0:
            self.log_text.delete("1.0", f"{excess}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _poll(self) -> None:
        self._drain_logs()
        while True:
            try:
                action: Any = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            action()
        if self.exit_requested.is_set():
            self.close()
        if self.closed.is_set():
            self.root.destroy()
            return
        self.root.after(120, self._poll)

    def close(self) -> None:
        """Zavření okna nebo Ctrl+C: hotspot se vždy nejdřív korektně zastaví."""
        if self.closing:
            return
        self.closing = True
        self._update_controls()
        self.state_pill.configure(text="UKONČUJI", bg=STATE_COLORS[HotspotState.STOPPING])

        def finish() -> None:
            self.controller.shutdown()
            self.closed.set()

        threading.Thread(target=finish, name="pocketap-close", daemon=True).start()
