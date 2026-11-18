"""Vlastní DHCPv4 server (RFC 2131) pro virtuální rozhraní AP.

Používá se jen tehdy, když DHCP neposkytuje Windows. Soket je navázaný na adresu
virtuálního rozhraní, takže server neodpovídá na ostatních rozhraních počítače.
"""
from __future__ import annotations

import ctypes
import errno
import ipaddress
import json
import os
import select
import socket
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import asdict, dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Callable, Iterator

from logger import get_logger

log = get_logger("dhcp")

DHCP_SERVER_PORT = 67
DHCP_CLIENT_PORT = 68
BROADCAST_ADDRESS = "255.255.255.255"
LEASES_PATH = Path(__file__).resolve().parent / "leases.json"

_MAGIC_COOKIE = b"\x63\x82\x53\x63"
_ZERO_IP = "0.0.0.0"
_MIN_REPLY_SIZE = 300
_IP_UNICAST_IF = 31


class MessageType(IntEnum):
    DISCOVER = 1
    OFFER = 2
    REQUEST = 3
    DECLINE = 4
    ACK = 5
    NAK = 6
    RELEASE = 7
    INFORM = 8


class Option(IntEnum):
    PAD = 0
    SUBNET_MASK = 1
    ROUTER = 3
    DNS = 6
    HOSTNAME = 12
    BROADCAST = 28
    REQUESTED_IP = 50
    LEASE_TIME = 51
    OVERLOAD = 52
    MESSAGE_TYPE = 53
    SERVER_ID = 54
    RENEWAL_T1 = 58
    REBINDING_T2 = 59
    END = 255


class DhcpPacketError(ValueError):
    """Přijatý paket není platná zpráva DHCP."""


class DhcpServerError(RuntimeError):
    """Server nelze spustit."""


def _ip_bytes(ip: str) -> bytes:
    return socket.inet_aton(ip)


def _ip_text(raw: bytes) -> str:
    return socket.inet_ntoa(raw)


def _parse_options(data: bytes) -> dict[int, bytes]:
    options: dict[int, bytes] = {}
    index = 0
    while index < len(data):
        code = data[index]
        index += 1
        if code == Option.PAD:
            continue
        if code == Option.END:
            break
        if index >= len(data):
            raise DhcpPacketError("Zkrácená volba DHCP.")
        length = data[index]
        index += 1
        if index + length > len(data):
            raise DhcpPacketError("Volba DHCP přesahuje délku paketu.")
        options[code] = options.get(code, b"") + data[index : index + length]
        index += length
    return options


@dataclass
class DhcpPacket:
    """Zpráva BOOTP/DHCP."""

    op: int = 1
    htype: int = 1
    hlen: int = 6
    hops: int = 0
    xid: int = 0
    secs: int = 0
    flags: int = 0
    ciaddr: str = _ZERO_IP
    yiaddr: str = _ZERO_IP
    siaddr: str = _ZERO_IP
    giaddr: str = _ZERO_IP
    chaddr: bytes = b"\x00" * 16
    options: dict[int, bytes] = field(default_factory=dict)

    @classmethod
    def parse(cls, data: bytes) -> "DhcpPacket":
        if len(data) < 240:
            raise DhcpPacketError("Paket je kratší než hlavička DHCP.")
        if data[236:240] != _MAGIC_COOKIE:
            raise DhcpPacketError("Chybí magic cookie DHCP.")
        op, htype, hlen, hops, xid, secs, flags = struct.unpack("!BBBBIHH", data[:12])
        options = _parse_options(data[240:])
        overload = options.get(Option.OVERLOAD, b"\x00")[0:1]
        if overload in (b"\x01", b"\x03"):
            for code, value in _parse_options(data[108:236]).items():
                options[code] = options.get(code, b"") + value
        if overload in (b"\x02", b"\x03"):
            for code, value in _parse_options(data[44:108]).items():
                options[code] = options.get(code, b"") + value
        return cls(
            op, htype, hlen, hops, xid, secs, flags,
            _ip_text(data[12:16]), _ip_text(data[16:20]), _ip_text(data[20:24]), _ip_text(data[24:28]),
            data[28:44], options,
        )

    def encode(self) -> bytes:
        header = struct.pack(
            "!BBBBIHH4s4s4s4s16s64s128s",
            self.op, self.htype, self.hlen, self.hops, self.xid, self.secs, self.flags,
            _ip_bytes(self.ciaddr), _ip_bytes(self.yiaddr), _ip_bytes(self.siaddr), _ip_bytes(self.giaddr),
            self.chaddr, b"", b"",
        )
        body = bytearray(_MAGIC_COOKIE)
        ordered = sorted(self.options, key=lambda code: (code != Option.MESSAGE_TYPE, code))
        for code in ordered:
            value = self.options[code]
            body += bytes((code, len(value))) + value
        body.append(Option.END)
        packet = header + bytes(body)
        return packet.ljust(_MIN_REPLY_SIZE, b"\x00")

    @property
    def mac(self) -> str:
        return "-".join(f"{octet:02X}" for octet in self.chaddr[: self.hlen])

    @property
    def message_type(self) -> int | None:
        raw = self.options.get(Option.MESSAGE_TYPE)
        return raw[0] if raw else None

    def _ip_option(self, code: int) -> str | None:
        raw = self.options.get(code)
        return _ip_text(raw[:4]) if raw and len(raw) >= 4 else None

    @property
    def requested_ip(self) -> str | None:
        return self._ip_option(Option.REQUESTED_IP)

    @property
    def server_id(self) -> str | None:
        return self._ip_option(Option.SERVER_ID)

    @property
    def hostname(self) -> str:
        raw = self.options.get(Option.HOSTNAME, b"")[:64].decode("utf-8", "replace")
        return "".join(ch for ch in raw if ch.isprintable())


@dataclass
class Lease:
    """Zápůjčka adresy pro jednu MAC adresu."""

    mac: str
    ip: str
    expires: float
    bound: bool = False
    hostname: str = ""


class LeasePool:
    """Evidence zápůjček. Třída je vláknově bezpečná a nezávislá na síti."""

    def __init__(
        self,
        start: str,
        end: str,
        lease_seconds: int,
        excluded: set[str] | None = None,
        offer_seconds: int = 60,
        conflict_seconds: int = 600,
        clock: Callable[[], float] = time.time,
        storage: Path | None = None,
    ) -> None:
        self._first = int(ipaddress.IPv4Address(start))
        self._last = int(ipaddress.IPv4Address(end))
        self.lease_seconds = lease_seconds
        self._excluded = excluded or set()
        self._offer_seconds = offer_seconds
        self._conflict_seconds = conflict_seconds
        self._clock = clock
        self._storage = storage
        self._lock = threading.RLock()
        self._leases: dict[str, Lease] = {}
        self._history: dict[str, str] = {}
        self._conflicts: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        if self._storage is None or not self._storage.exists():
            return
        try:
            raw = json.loads(self._storage.read_text(encoding="utf-8"))
            now = self._clock()
            for item in raw.get("leases", []):
                lease = Lease(str(item["mac"]), str(item["ip"]), float(item["expires"]), True, str(item.get("hostname", "")))
                if self._in_range(lease.ip):
                    self._history[lease.mac] = lease.ip
                    if lease.expires > now:
                        self._leases[lease.mac] = lease
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            log.warning("Soubor zápůjček nelze načíst (%s), začínám s prázdnou evidencí.", exc)

    def _save(self) -> None:
        if self._storage is None:
            return
        bound = [asdict(lease) for lease in self._leases.values() if lease.bound]
        temporary = self._storage.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps({"leases": bound}, indent=2), encoding="utf-8")
            os.replace(temporary, self._storage)
        except OSError as exc:
            log.warning("Soubor zápůjček nelze uložit: %s", exc)

    def _in_range(self, ip: str) -> bool:
        try:
            value = int(ipaddress.IPv4Address(ip))
        except ValueError:
            return False
        return self._first <= value <= self._last and ip not in self._excluded

    def _owner(self, ip: str) -> str | None:
        return next((l.mac for l in self._leases.values() if l.ip == ip), None)

    def _is_available(self, mac: str, ip: str) -> bool:
        if not self._in_range(ip) or self._conflicts.get(ip, 0) > self._clock():
            return False
        return self._owner(ip) in (None, mac)

    def expire(self) -> list[Lease]:
        """Odstraní propadlé zápůjčky a vrátí je."""
        with self._lock:
            now = self._clock()
            expired = [lease for lease in self._leases.values() if lease.expires <= now]
            for lease in expired:
                del self._leases[lease.mac]
            self._conflicts = {ip: until for ip, until in self._conflicts.items() if until > now}
            if any(lease.bound for lease in expired):
                self._save()
            return expired

    def candidates(self, mac: str, requested: str | None = None) -> Iterator[str]:
        """Vrací vhodné adresy: současnou, požadovanou, předchozí a pak první volné."""
        self.expire()
        with self._lock:
            current = self._leases.get(mac)
            preferred = [current.ip if current else None, requested, self._history.get(mac)]
            ordered = [ip for ip in preferred if ip]
            ordered += [str(ipaddress.IPv4Address(v)) for v in range(self._first, self._last + 1)]
            result = [ip for ip in dict.fromkeys(ordered) if self._is_available(mac, ip)]
        yield from result

    def offer(self, mac: str, ip: str, hostname: str = "") -> Lease | None:
        """Dočasně rezervuje adresu po dobu nabídky."""
        with self._lock:
            if not self._is_available(mac, ip):
                return None
            existing = self._leases.get(mac)
            if existing and existing.bound and existing.ip == ip:
                return existing
            lease = Lease(mac, ip, self._clock() + self._offer_seconds, False, hostname)
            self._leases[mac] = lease
            return lease

    def bind(self, mac: str, ip: str, hostname: str = "") -> Lease | None:
        """Potvrdí zápůjčku nebo ji obnoví. Vrací None, pokud adresa není dostupná."""
        with self._lock:
            self.expire()
            if not self._is_available(mac, ip):
                return None
            previous = self._leases.get(mac)
            name = hostname or (previous.hostname if previous else "")
            lease = Lease(mac, ip, self._clock() + self.lease_seconds, True, name)
            self._leases[mac] = lease
            self._history[mac] = ip
            self._save()
            return lease

    def forget_offer(self, mac: str) -> None:
        """Klient si vybral jiný server, nabídka se zruší."""
        with self._lock:
            lease = self._leases.get(mac)
            if lease and not lease.bound:
                del self._leases[mac]

    def release(self, mac: str, ip: str) -> bool:
        """Uvolní adresu na žádost klienta."""
        with self._lock:
            lease = self._leases.get(mac)
            if lease is None or lease.ip != ip:
                return False
            del self._leases[mac]
            self._save()
            return True

    def mark_conflict(self, ip: str) -> None:
        """Dočasně vyřadí adresu, kterou používá jiné zařízení."""
        with self._lock:
            self._conflicts[ip] = self._clock() + self._conflict_seconds

    def decline(self, mac: str, ip: str) -> None:
        """Klient zjistil kolizi adresy (DHCPDECLINE)."""
        with self._lock:
            lease = self._leases.get(mac)
            if lease and lease.ip == ip:
                del self._leases[mac]
                self._save()
            self._history.pop(mac, None)
            self.mark_conflict(ip)

    def active(self) -> list[Lease]:
        """Vrátí platné potvrzené zápůjčky."""
        self.expire()
        with self._lock:
            return [lease for lease in self._leases.values() if lease.bound]


class ArpConflictProbe:
    """Ověřuje kolize adres přes ARP (iphlpapi.SendARP) bez oprávnění správce."""

    def __init__(self, source_ip: str, timeout_ms: int = 800) -> None:
        self._source = struct.unpack("<I", _ip_bytes(source_ip))[0]
        self._timeout = timeout_ms / 1000
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="pocketap-arp")
        self._send_arp = ctypes.WinDLL("iphlpapi").SendARP
        self._send_arp.argtypes = (
            ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong),
        )
        self._send_arp.restype = ctypes.c_ulong

    def _lookup(self, ip: str) -> str | None:
        target = struct.unpack("<I", _ip_bytes(ip))[0]
        buffer = ctypes.create_string_buffer(8)
        length = ctypes.c_ulong(6)
        result = self._send_arp(target, self._source, buffer, ctypes.byref(length))
        if result != 0 or length.value != 6:
            return None
        return "-".join(f"{octet:02X}" for octet in buffer.raw[:6])

    def in_use(self, ip: str, mac: str, on_late_conflict: Callable[[str], None]) -> bool:
        """Vrátí True, pokud adresu používá jiné zařízení než dotazující se klient."""
        future = self._executor.submit(self._lookup, ip)
        try:
            owner = future.result(timeout=self._timeout)
        except FutureTimeout:
            # Volná adresa na ARP neodpoví. Pozdní odpověď adresu dodatečně vyřadí.
            def late(done: "object") -> None:
                try:
                    found = future.result()
                except Exception:  # noqa: BLE001 - pozadí nesmí shodit server
                    return
                if found and found != mac:
                    on_late_conflict(ip)

            future.add_done_callback(late)
            return False
        return bool(owner and owner != mac)

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


ConflictCheck = Callable[[str, str], bool]


@dataclass(frozen=True)
class DhcpReply:
    """Odpověď a cílová adresa."""

    packet: DhcpPacket
    address: str


class DhcpServer:
    """Server obsluhující DISCOVER, REQUEST, RELEASE, DECLINE a INFORM."""

    def __init__(
        self,
        server_ip: str,
        netmask: str,
        pool: LeasePool,
        if_index: int | None = None,
        advertise_gateway: bool = False,
        conflict_timeout_ms: int = 800,
        conflict_check: ConflictCheck | None = None,
        server_port: int = DHCP_SERVER_PORT,
        client_port: int = DHCP_CLIENT_PORT,
        broadcast_address: str = BROADCAST_ADDRESS,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self.server_ip = server_ip
        self.netmask = netmask
        self.pool = pool
        self._network = ipaddress.IPv4Network(f"{server_ip}/{netmask}", strict=False)
        self._if_index = if_index
        self._advertise_gateway = advertise_gateway
        self._conflict_timeout_ms = conflict_timeout_ms
        self._conflict_check = conflict_check
        self._probe: ArpConflictProbe | None = None
        self._server_port = server_port
        self._client_port = client_port
        self._broadcast = broadcast_address
        self._on_change = on_change
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def _is_conflict(self, ip: str, mac: str) -> bool:
        if self._conflict_check is not None:
            return self._conflict_check(ip, mac)
        if self._probe is None:
            return False
        return self._probe.in_use(ip, mac, self.pool.mark_conflict)

    def _changed(self) -> None:
        if self._on_change:
            self._on_change()

    def _reply(self, request: DhcpPacket, kind: MessageType, yiaddr: str = _ZERO_IP, with_lease: bool = False) -> DhcpPacket:
        options: dict[int, bytes] = {
            Option.MESSAGE_TYPE: bytes((kind,)),
            Option.SERVER_ID: _ip_bytes(self.server_ip),
        }
        if kind != MessageType.NAK:
            options[Option.SUBNET_MASK] = _ip_bytes(self.netmask)
            options[Option.BROADCAST] = _ip_bytes(str(self._network.broadcast_address))
            if self._advertise_gateway:
                options[Option.ROUTER] = _ip_bytes(self.server_ip)
                options[Option.DNS] = _ip_bytes(self.server_ip)
        if with_lease:
            seconds = self.pool.lease_seconds
            options[Option.LEASE_TIME] = struct.pack("!I", seconds)
            options[Option.RENEWAL_T1] = struct.pack("!I", seconds // 2)
            options[Option.REBINDING_T2] = struct.pack("!I", seconds * 7 // 8)
        return DhcpPacket(
            op=2, htype=request.htype, hlen=request.hlen, xid=request.xid, flags=request.flags,
            ciaddr=request.ciaddr if kind == MessageType.ACK else _ZERO_IP,
            yiaddr=yiaddr, siaddr=self.server_ip if kind != MessageType.NAK else _ZERO_IP,
            giaddr=request.giaddr, chaddr=request.chaddr, options=options,
        )

    def _destination(self, request: DhcpPacket, kind: MessageType) -> str:
        # Klient bez adresy ještě neodpovídá na ARP, proto se používá broadcast.
        if kind != MessageType.NAK and request.ciaddr != _ZERO_IP:
            return request.ciaddr
        return self._broadcast

    def handle_packet(self, request: DhcpPacket) -> DhcpReply | None:
        """Zpracuje zprávu a vrátí odpověď, pokud se má odeslat."""
        if request.op != 1 or request.htype != 1 or request.hlen != 6 or request.giaddr != _ZERO_IP:
            return None
        if request.mac in {"00-00-00-00-00-00", "FF-FF-FF-FF-FF-FF"}:
            return None
        handlers = {
            MessageType.DISCOVER: self._on_discover,
            MessageType.REQUEST: self._on_request,
            MessageType.RELEASE: self._on_release,
            MessageType.DECLINE: self._on_decline,
            MessageType.INFORM: self._on_inform,
        }
        handler = handlers.get(request.message_type)  # type: ignore[arg-type]
        return handler(request) if handler else None

    def _on_discover(self, request: DhcpPacket) -> DhcpReply | None:
        for attempt, ip in enumerate(self.pool.candidates(request.mac, request.requested_ip)):
            if attempt >= 8:
                break
            if self._is_conflict(ip, request.mac):
                log.warning("Adresa %s je obsazená jiným zařízením, přeskakuji ji.", ip)
                self.pool.mark_conflict(ip)
                continue
            if self.pool.offer(request.mac, ip, request.hostname):
                log.info("DHCPOFFER %s pro %s", ip, request.mac)
                return DhcpReply(self._reply(request, MessageType.OFFER, ip, True), self._broadcast)
        log.warning("Rozsah DHCP je vyčerpaný, klient %s nedostal adresu.", request.mac)
        return None

    def _nak(self, request: DhcpPacket, reason: str) -> DhcpReply:
        log.info("DHCPNAK pro %s: %s", request.mac, reason)
        return DhcpReply(self._reply(request, MessageType.NAK), self._broadcast)

    def _on_request(self, request: DhcpPacket) -> DhcpReply | None:
        server_id = request.server_id
        if server_id is not None and server_id != self.server_ip:
            self.pool.forget_offer(request.mac)
            return None
        ip = request.requested_ip or (request.ciaddr if request.ciaddr != _ZERO_IP else None)
        if ip is None:
            return None
        lease = self.pool.bind(request.mac, ip, request.hostname)
        if lease is None:
            return self._nak(request, f"adresa {ip} není dostupná")
        log.info("DHCPACK %s pro %s %s", ip, request.mac, f"({lease.hostname})" if lease.hostname else "")
        self._changed()
        reply = self._reply(request, MessageType.ACK, ip, True)
        return DhcpReply(reply, self._destination(request, MessageType.ACK))

    def _on_release(self, request: DhcpPacket) -> None:
        if request.server_id in (None, self.server_ip) and self.pool.release(request.mac, request.ciaddr):
            log.info("DHCPRELEASE %s od %s", request.ciaddr, request.mac)
            self._changed()

    def _on_decline(self, request: DhcpPacket) -> None:
        if request.server_id in (None, self.server_ip) and request.requested_ip:
            log.warning("DHCPDECLINE %s od %s, adresa je dočasně vyřazena.", request.requested_ip, request.mac)
            self.pool.decline(request.mac, request.requested_ip)
            self._changed()

    def _on_inform(self, request: DhcpPacket) -> DhcpReply | None:
        if request.ciaddr == _ZERO_IP:
            return None
        return DhcpReply(self._reply(request, MessageType.ACK), request.ciaddr)

    def _open_socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            if self._if_index:
                sock.setsockopt(socket.IPPROTO_IP, _IP_UNICAST_IF, socket.htonl(self._if_index))
            sock.bind((self.server_ip, self._server_port))
            sock.setblocking(False)
            return sock
        except OSError as exc:
            sock.close()
            if exc.errno in (errno.EACCES, errno.EADDRINUSE) or getattr(exc, "winerror", 0) in (10013, 10048):
                raise DhcpServerError(
                    f"Port UDP {self._server_port} na {self.server_ip} je obsazený jiným DHCP serverem."
                ) from exc
            raise DhcpServerError(f"DHCP soket nelze otevřít na {self.server_ip}: {exc}") from exc

    def start(self) -> None:
        """Otevře soket a spustí obslužné vlákno."""
        if self._thread is not None:
            return
        self._stop.clear()
        self._socket = self._open_socket()
        if self._conflict_check is None:
            self._probe = ArpConflictProbe(self.server_ip, self._conflict_timeout_ms)
        self._thread = threading.Thread(target=self._serve, name="pocketap-dhcp", daemon=True)
        self._thread.start()
        log.info("DHCP server poslouchá na %s:%d.", self.server_ip, self._server_port)

    def _serve(self) -> None:
        assert self._socket is not None
        sock = self._socket
        next_expiry = time.monotonic() + 30
        while not self._stop.is_set():
            try:
                readable, _, _ = select.select([sock], [], [], 0.5)
                if readable:
                    self._process_datagram(sock)
            except OSError as exc:
                if self._stop.is_set():
                    break
                # WSAECONNRESET po ICMP chybě nesmí server ukončit.
                log.debug("Chyba soketu DHCP: %s", exc)
            if time.monotonic() >= next_expiry:
                next_expiry = time.monotonic() + 30
                if self.pool.expire():
                    self._changed()

    def _process_datagram(self, sock: socket.socket) -> None:
        data, source = sock.recvfrom(4096)
        try:
            request = DhcpPacket.parse(data)
        except DhcpPacketError as exc:
            log.debug("Neplatný paket DHCP od %s: %s", source[0], exc)
            return
        reply = self.handle_packet(request)
        if reply is not None:
            sock.sendto(reply.packet.encode(), (reply.address, self._client_port))

    def stop(self) -> None:
        """Ukončí vlákno a uvolní soket."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        if self._probe is not None:
            self._probe.close()
            self._probe = None
        log.info("DHCP server zastaven.")
