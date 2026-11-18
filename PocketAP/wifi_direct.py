"""Ovládání Wi-Fi Direct Legacy AP přes WinRT API Windows.Devices.WiFiDirect.

Postup odpovídá oficiálnímu vzoru Microsoftu WiFiDirectLegacyAP:
publisher -> IsAutonomousGroupOwnerEnabled -> LegacySettings -> Start ->
po stavu Started ConnectionListener -> WiFiDirectDevice.FromIdAsync pro klienty.

Hosted Network (netsh wlan set hostednetwork, WlanHostedNetwork*) se nepoužívá.
"""
from __future__ import annotations

import asyncio
import importlib
import threading
import time
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Callable

from logger import get_logger

log = get_logger("wifidirect")

_HRESULT_HINTS: dict[int, str] = {
    0x80070005: "přístup odepřen (zkontrolujte oprávnění aplikací k Wi-Fi a zásady organizace)",
    0x8007001F: "ovladač Wi-Fi nahlásil obecnou chybu zařízení",
    0x80070032: "ovladač nebo Windows tento režim nepodporuje",
    0x80070057: "neplatné SSID nebo heslo",
    0x800704DF: "Wi-Fi služba není připravena",
    0x8007139F: "Wi-Fi Direct je v neplatném stavu (AP už může běžet v jiné aplikaci)",
    0x80004005: "nespecifikovaná chyba Wi-Fi Direct API",
}


class WiFiDirectApError(RuntimeError):
    """Chyba Wi-Fi Direct API s konkrétním popisem pro uživatele."""


def describe_exception(exc: BaseException) -> str:
    """Převede výjimku WinRT na čitelný text včetně kódu HRESULT."""
    code = getattr(exc, "winerror", None)
    if code is None:
        return str(exc) or exc.__class__.__name__
    hresult = int(code) & 0xFFFFFFFF
    hint = _HRESULT_HINTS.get(hresult, str(exc))
    return f"{hint} (HRESULT 0x{hresult:08X})"


@dataclass(frozen=True)
class ConnectedClient:
    """Klient potvrzený přes WiFiDirectDevice."""

    device_id: str
    name: str
    remote_ip: str | None


class _Bindings:
    """Líně načtené moduly WinRT, aby chybějící balíček dal srozumitelnou chybu."""

    def __init__(self) -> None:
        try:
            self.wifidirect: ModuleType = importlib.import_module("winrt.windows.devices.wifidirect")
            self.credentials: ModuleType = importlib.import_module("winrt.windows.security.credentials")
            self.enumeration: ModuleType = importlib.import_module("winrt.windows.devices.enumeration")
            importlib.import_module("winrt.windows.foundation")
            importlib.import_module("winrt.windows.foundation.collections")
            importlib.import_module("winrt.windows.networking")
        except ImportError as exc:
            raise WiFiDirectApError(
                "Chybí Python balíčky WinRT. Spusťte run.bat nebo "
                f"'pip install -r requirements.txt'. Detail: {exc}"
            ) from exc


def count_wifi_direct_device_interfaces() -> int:
    """Vrátí počet rozhraní Wi-Fi Direct, která Windows hlásí."""
    bindings = _Bindings()
    wd = bindings.wifidirect

    async def query() -> int:
        selector = wd.WiFiDirectDevice.get_device_selector(wd.WiFiDirectDeviceSelectorType.DEVICE_INTERFACE)
        devices = await bindings.enumeration.DeviceInformation.find_all_async(selector)
        return len(devices)

    try:
        return asyncio.run(query())
    except OSError as exc:
        raise WiFiDirectApError(f"Výčet zařízení Wi-Fi Direct selhal: {describe_exception(exc)}") from exc


class WiFiDirectAccessPoint:
    """Jedna instance běžícího Wi-Fi Direct Legacy AP (Group Owner)."""

    def __init__(
        self,
        on_aborted: Callable[[str], None] | None = None,
        on_clients_changed: Callable[[], None] | None = None,
    ) -> None:
        self._bindings = _Bindings()
        self._on_aborted = on_aborted
        self._on_clients_changed = on_clients_changed
        self._lock = threading.RLock()
        self._status_changed = threading.Condition(self._lock)
        self._publisher: Any = None
        self._status_token: Any = None
        self._listener: Any = None
        self._listener_token: Any = None
        self._status_name = "CREATED"
        self._failure: str | None = None
        self._stopping = False
        self._devices: dict[str, tuple[Any, Any, ConnectedClient]] = {}

    @property
    def failure(self) -> str | None:
        """Text chyby, pokud Windows AP přerušil."""
        with self._lock:
            return self._failure

    @property
    def clients(self) -> list[ConnectedClient]:
        """Klienti, které Wi-Fi Direct API aktuálně hlásí jako připojené."""
        with self._lock:
            return [entry[2] for entry in self._devices.values()]

    def _error_text(self, error: int) -> str:
        names = self._bindings.wifidirect.WiFiDirectError
        if error == names.RADIO_NOT_AVAILABLE:
            return (
                "Wi-Fi rádio není dostupné (RadioNotAvailable). Zapněte Wi-Fi a vypněte režim "
                "Letadlo."
            )
        if error == names.RESOURCE_IN_USE:
            return (
                "Prostředky Wi-Fi Direct jsou obsazené (ResourceInUse). Vypněte Mobile Hotspot, "
                "Miracast nebo jinou aplikaci používající Wi-Fi Direct."
            )
        return f"Windows přerušil Wi-Fi Direct AP (kód WiFiDirectError {int(error)})."

    def _handle_status(self, _sender: Any, args: Any) -> None:
        """Událost StatusChanged přichází z vlákna WinRT."""
        statuses = self._bindings.wifidirect.WiFiDirectAdvertisementPublisherStatus
        status = args.status
        notify: str | None = None
        with self._status_changed:
            self._status_name = statuses(status).name
            log.debug("Stav publisheru: %s", self._status_name)
            if status == statuses.ABORTED:
                self._failure = self._error_text(args.error)
                notify = self._failure
            self._status_changed.notify_all()
        if notify and not self._stopping and self._on_aborted:
            self._on_aborted(notify)

    def start(self, ssid: str, passphrase: str, timeout: float = 25.0) -> None:
        """Spustí AP a vrátí se až po potvrzení stavu Started."""
        wd = self._bindings.wifidirect
        try:
            publisher = wd.WiFiDirectAdvertisementPublisher()
            self._publisher = publisher
            self._status_token = publisher.add_status_changed(self._handle_status)

            advertisement = publisher.advertisement
            advertisement.is_autonomous_group_owner_enabled = True
            legacy = advertisement.legacy_settings
            legacy.is_enabled = True
            legacy.ssid = ssid
            credential = legacy.passphrase
            credential.password = passphrase
            legacy.passphrase = credential

            if legacy.ssid != ssid or legacy.passphrase.password != passphrase:
                raise WiFiDirectApError("Windows nepřijal zadané SSID nebo heslo.")
            publisher.start()
        except OSError as exc:
            self._release_publisher()
            raise WiFiDirectApError(f"Spuštění Wi-Fi Direct selhalo: {describe_exception(exc)}") from exc

        self._wait_for_started(timeout)
        try:
            self._start_listener()
        except OSError as exc:
            try:
                self.stop()
            except WiFiDirectApError as stop_error:
                log.debug("Úklid po chybě listeneru: %s", stop_error)
            raise WiFiDirectApError(
                f"Příjem klientů nelze spustit (WiFiDirectConnectionListener): {describe_exception(exc)}"
            ) from exc
        log.info("Wi-Fi Direct Legacy AP běží, SSID '%s'.", ssid)

    def _wait_for_started(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        with self._status_changed:
            while self._status_name not in {"STARTED", "ABORTED", "STOPPED"}:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._status_changed.wait(remaining)
            status, failure = self._status_name, self._failure

        if status == "STARTED":
            return
        try:
            self.stop()
        except WiFiDirectApError as exc:  # Původní příčina je pro uživatele důležitější.
            log.debug("Úklid po neúspěšném startu: %s", exc)
        if failure:
            raise WiFiDirectApError(failure)
        if status == "STOPPED":
            raise WiFiDirectApError("Windows AP ihned po spuštění zastavil.")
        raise WiFiDirectApError(
            f"Windows do {timeout:.0f} s nepotvrdil spuštění AP. Ovladač pravděpodobně "
            "režim Wi-Fi Direct Group Owner nepodporuje."
        )

    def _start_listener(self) -> None:
        listener = self._bindings.wifidirect.WiFiDirectConnectionListener()
        self._listener_token = listener.add_connection_requested(self._handle_connection_requested)
        self._listener = listener

    def _handle_connection_requested(self, _sender: Any, args: Any) -> None:
        try:
            request = args.get_connection_request()
            info = request.device_information
            device_id, name = info.id, info.name
        except OSError as exc:
            log.warning("Žádost o připojení nelze přečíst: %s", describe_exception(exc))
            return
        threading.Thread(
            target=self._accept_client,
            args=(request, device_id, name),
            name="pocketap-accept",
            daemon=True,
        ).start()

    def _accept_client(self, request: Any, device_id: str, name: str) -> None:
        """Přijme klienta stejně jako vzor Microsoftu přes FromIdAsync."""
        wd = self._bindings.wifidirect

        async def open_device() -> Any:
            return await wd.WiFiDirectDevice.from_id_async(device_id)

        try:
            device = asyncio.run(open_device())
            if device is None:
                log.warning("Windows odmítl otevřít klienta '%s'.", name)
                return
            remote_ip: str | None = None
            pairs = device.get_connection_endpoint_pairs()
            if len(pairs) > 0:
                remote_ip = pairs[0].remote_host_name.display_name
            token = device.add_connection_status_changed(self._handle_device_status)
            client = ConnectedClient(device_id, name, remote_ip)
            with self._lock:
                if self._stopping:
                    device.close()
                    return
                self._devices[device_id] = (device, token, client)
            log.info("Klient připojen: %s (%s)", name or "bez názvu", remote_ip or "IP zatím neznámá")
            self._notify_clients()
        except OSError as exc:
            log.warning("Klienta '%s' nelze přijmout: %s", name, describe_exception(exc))
        finally:
            try:
                request.close()
            except OSError:
                pass

    def _handle_device_status(self, sender: Any, _args: Any) -> None:
        wd = self._bindings.wifidirect
        try:
            if sender.connection_status != wd.WiFiDirectConnectionStatus.DISCONNECTED:
                return
            device_id = sender.device_id
        except OSError:
            return
        with self._lock:
            entry = self._devices.pop(device_id, None)
        if entry:
            self._close_device(entry[0], entry[1])
            log.info("Klient odpojen: %s", entry[2].name or device_id)
            self._notify_clients()

    @staticmethod
    def _close_device(device: Any, token: Any) -> None:
        try:
            device.remove_connection_status_changed(token)
            device.close()
        except OSError as exc:
            log.debug("Zavření klienta: %s", describe_exception(exc))

    def _notify_clients(self) -> None:
        if self._on_clients_changed:
            self._on_clients_changed()

    def stop(self, timeout: float = 12.0) -> None:
        """Zastaví advertisement a ukončí roli Group Owner. Lze volat opakovaně."""
        self._stopping = True
        if self._listener is not None:
            try:
                self._listener.remove_connection_requested(self._listener_token)
            except OSError as exc:
                log.debug("Odebrání listeneru: %s", describe_exception(exc))
            self._listener = None

        with self._lock:
            devices = list(self._devices.values())
            self._devices.clear()
        for device, token, _client in devices:
            self._close_device(device, token)

        publisher = self._publisher
        if publisher is None:
            return
        try:
            with self._lock:
                needs_stop = self._status_name in {"STARTED", "CREATED"}
            if needs_stop:
                publisher.stop()
                self._wait_for_stopped(timeout)
        except OSError as exc:
            raise WiFiDirectApError(f"Zastavení AP selhalo: {describe_exception(exc)}") from exc
        finally:
            self._release_publisher()

    def _wait_for_stopped(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        with self._status_changed:
            while self._status_name not in {"STOPPED", "ABORTED"}:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WiFiDirectApError(f"Windows do {timeout:.0f} s nepotvrdil zastavení AP.")
                self._status_changed.wait(remaining)

    def _release_publisher(self) -> None:
        publisher, token = self._publisher, self._status_token
        self._publisher = None
        self._status_token = None
        if publisher is not None and token is not None:
            try:
                publisher.remove_status_changed(token)
            except OSError as exc:
                log.debug("Odebrání události: %s", describe_exception(exc))
