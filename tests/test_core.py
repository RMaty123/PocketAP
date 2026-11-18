"""Automatické testy logiky, které nevyžadují Wi-Fi hardware ani práva správce.

Spuštění ze složky PocketAP:
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import ipaddress
import socket
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import adapter_manager as am  # noqa: E402
import firewall as fw  # noqa: E402
import network_config as nc  # noqa: E402
from dhcp_server import DhcpPacket, DhcpPacketError, DhcpServer, LeasePool, MessageType, Option  # noqa: E402
from runtime_state import RuntimeState  # noqa: E402
from settings import (  # noqa: E402
    ConfigError, NetworkSettings, generate_passphrase, load_config, parse_config,
    validate_passphrase, validate_ssid,
)
from system_utils import CommandResult  # noqa: E402

MAC_A = bytes.fromhex("02aabbccdd01").ljust(16, b"\x00")
MAC_B = bytes.fromhex("02aabbccdd02").ljust(16, b"\x00")
SERVER = "192.168.50.1"


def client_packet(kind: MessageType, chaddr: bytes = MAC_A, **fields: object) -> DhcpPacket:
    options = {Option.MESSAGE_TYPE: bytes((kind,))}
    options.update(fields.pop("options", {}))  # type: ignore[arg-type]
    return DhcpPacket(xid=0x1234, chaddr=chaddr, options=options, **fields)  # type: ignore[arg-type]


def ip_option(ip: str) -> bytes:
    return socket.inet_aton(ip)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def make_server(clock: FakeClock | None = None, in_use: set[str] | None = None, gateway: bool = False) -> DhcpServer:
    pool = LeasePool("192.168.50.100", "192.168.50.102", 43200, excluded={SERVER}, clock=clock or FakeClock())
    taken = in_use or set()
    return DhcpServer(
        SERVER, "255.255.255.0", pool, advertise_gateway=gateway, conflict_check=lambda ip, _mac: ip in taken
    )


class SettingsTests(unittest.TestCase):
    def test_defaults_from_config_file(self) -> None:
        config = load_config()
        self.assertEqual((config.ssid, config.passphrase), ("PocketAP", "PocketAP12345"))
        self.assertEqual(config.network.host_ip, "192.168.50.1")
        self.assertEqual(config.network.lease_seconds, 12 * 3600)
        self.assertFalse(config.dhcp.advertise_host_as_gateway)

    def test_ssid_limits(self) -> None:
        self.assertEqual(validate_ssid("a" * 32), "a" * 32)
        for invalid in ("", "   ", "a" * 33, "č" * 17, "a\nb"):
            with self.assertRaises(ConfigError):
                validate_ssid(invalid)

    def test_passphrase_limits(self) -> None:
        self.assertEqual(validate_passphrase("12345678"), "12345678")
        for invalid in ("1234567", "x" * 64, "heslíčko123"):
            with self.assertRaises(ConfigError):
                validate_passphrase(invalid)

    def test_generated_passphrase_is_valid_and_unique(self) -> None:
        values = {generate_passphrase() for _ in range(20)}
        self.assertEqual(len(values), 20)
        for value in values:
            validate_passphrase(value)

    def test_rejects_bad_network_plans(self) -> None:
        bad = (
            NetworkSettings(dhcp_start="192.168.51.100"),
            NetworkSettings(dhcp_start="192.168.50.200", dhcp_end="192.168.50.100"),
            NetworkSettings(host_ip="192.168.50.150"),
            NetworkSettings(dhcp_end="192.168.50.255"),
            NetworkSettings(lease_seconds=10),
        )
        for network in bad:
            with self.assertRaises(ConfigError):
                network.validate()

    def test_rejects_invalid_config_values(self) -> None:
        with self.assertRaises(ConfigError):
            parse_config({"network": {"host_ip": "not-an-ip"}})
        with self.assertRaises(ConfigError):
            parse_config({"test_server": {"http_port": 70000}})


class PacketTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        packet = client_packet(MessageType.REQUEST, options={Option.REQUESTED_IP: ip_option("192.168.50.100")})
        decoded = DhcpPacket.parse(packet.encode())
        self.assertEqual(decoded.message_type, MessageType.REQUEST)
        self.assertEqual(decoded.requested_ip, "192.168.50.100")
        self.assertEqual(decoded.mac, "02-AA-BB-CC-DD-01")
        self.assertEqual(decoded.xid, 0x1234)

    def test_rejects_malformed_packets(self) -> None:
        valid = client_packet(MessageType.DISCOVER).encode()
        samples = (b"", valid[:100], valid[:236] + b"\x00\x00\x00\x00" + valid[240:], valid[:240] + bytes((53, 200, 1)))
        for sample in samples:
            with self.assertRaises(DhcpPacketError):
                DhcpPacket.parse(sample)

    def test_reply_has_minimum_bootp_size(self) -> None:
        self.assertGreaterEqual(len(client_packet(MessageType.DISCOVER).encode()), 300)


class DhcpFlowTests(unittest.TestCase):
    def dora(self, server: DhcpServer, chaddr: bytes = MAC_A) -> str:
        offer = server.handle_packet(client_packet(MessageType.DISCOVER, chaddr))
        self.assertIsNotNone(offer)
        assert offer is not None
        self.assertEqual(offer.packet.message_type, MessageType.OFFER)
        ip = offer.packet.yiaddr
        ack = server.handle_packet(
            client_packet(
                MessageType.REQUEST, chaddr,
                options={Option.REQUESTED_IP: ip_option(ip), Option.SERVER_ID: ip_option(SERVER)},
            )
        )
        assert ack is not None
        self.assertEqual(ack.packet.message_type, MessageType.ACK)
        self.assertEqual(ack.packet.yiaddr, ip)
        return ip

    def test_full_exchange_and_options(self) -> None:
        server = make_server()
        offer = server.handle_packet(client_packet(MessageType.DISCOVER))
        assert offer is not None
        options = offer.packet.options
        self.assertEqual(offer.address, "255.255.255.255")
        self.assertEqual(offer.packet.yiaddr, "192.168.50.100")
        self.assertEqual(options[Option.SERVER_ID], ip_option(SERVER))
        self.assertEqual(options[Option.SUBNET_MASK], ip_option("255.255.255.0"))
        self.assertEqual(struct.unpack("!I", options[Option.LEASE_TIME])[0], 43200)
        self.assertEqual(struct.unpack("!I", options[Option.RENEWAL_T1])[0], 21600)

    def test_isolated_mode_sends_no_gateway_or_dns(self) -> None:
        offer = make_server().handle_packet(client_packet(MessageType.DISCOVER))
        assert offer is not None
        self.assertNotIn(Option.ROUTER, offer.packet.options)
        self.assertNotIn(Option.DNS, offer.packet.options)

    def test_compatibility_option_advertises_host(self) -> None:
        offer = make_server(gateway=True).handle_packet(client_packet(MessageType.DISCOVER))
        assert offer is not None
        self.assertEqual(offer.packet.options[Option.ROUTER], ip_option(SERVER))

    def test_same_mac_keeps_address_and_macs_differ(self) -> None:
        server = make_server()
        first = self.dora(server)
        second = self.dora(server, MAC_B)
        self.assertNotEqual(first, second)
        self.assertEqual(self.dora(server), first)

    def test_renewal_extends_lease_and_is_unicast(self) -> None:
        clock = FakeClock()
        server = make_server(clock)
        ip = self.dora(server)
        clock.now += 21600
        reply = server.handle_packet(client_packet(MessageType.REQUEST, ciaddr=ip))
        assert reply is not None
        self.assertEqual(reply.packet.message_type, MessageType.ACK)
        self.assertEqual(reply.address, ip)
        self.assertEqual(server.pool.active()[0].expires, clock.now + 43200)

    def test_expired_lease_is_removed_and_reusable(self) -> None:
        clock = FakeClock()
        server = make_server(clock)
        self.dora(server)
        clock.now += 43201
        self.assertEqual(server.pool.active(), [])
        self.assertEqual(self.dora(server, MAC_B), "192.168.50.100")

    def test_release_frees_address(self) -> None:
        server = make_server()
        ip = self.dora(server)
        server.handle_packet(client_packet(MessageType.RELEASE, ciaddr=ip, options={Option.SERVER_ID: ip_option(SERVER)}))
        self.assertEqual(server.pool.active(), [])

    def test_request_for_foreign_address_gets_nak(self) -> None:
        server = make_server()
        ip = self.dora(server, MAC_A)
        reply = server.handle_packet(client_packet(MessageType.REQUEST, MAC_B, options={Option.REQUESTED_IP: ip_option(ip)}))
        assert reply is not None
        self.assertEqual(reply.packet.message_type, MessageType.NAK)

    def test_request_outside_range_gets_nak(self) -> None:
        reply = make_server().handle_packet(
            client_packet(MessageType.REQUEST, options={Option.REQUESTED_IP: ip_option("10.0.0.5")})
        )
        assert reply is not None
        self.assertEqual(reply.packet.message_type, MessageType.NAK)

    def test_client_choosing_other_server_is_ignored(self) -> None:
        server = make_server()
        server.handle_packet(client_packet(MessageType.DISCOVER))
        reply = server.handle_packet(
            client_packet(
                MessageType.REQUEST,
                options={Option.REQUESTED_IP: ip_option("192.168.50.100"), Option.SERVER_ID: ip_option("192.168.50.2")},
            )
        )
        self.assertIsNone(reply)
        self.assertEqual(self.dora(server, MAC_B), "192.168.50.100")

    def test_conflicting_address_is_skipped(self) -> None:
        server = make_server(in_use={"192.168.50.100"})
        self.assertEqual(self.dora(server), "192.168.50.101")

    def test_decline_quarantines_address(self) -> None:
        server = make_server()
        ip = self.dora(server)
        server.handle_packet(client_packet(MessageType.DECLINE, options={Option.REQUESTED_IP: ip_option(ip)}))
        self.assertNotEqual(self.dora(server), ip)

    def test_exhausted_pool_gives_no_offer(self) -> None:
        server = make_server()
        for last in (1, 2, 3):
            self.dora(server, bytes.fromhex(f"02aabbccee0{last}").ljust(16, b"\x00"))
        self.assertIsNone(server.handle_packet(client_packet(MessageType.DISCOVER, MAC_A)))

    def test_ignores_relayed_and_invalid_clients(self) -> None:
        server = make_server()
        self.assertIsNone(server.handle_packet(client_packet(MessageType.DISCOVER, giaddr="10.0.0.1")))
        self.assertIsNone(server.handle_packet(client_packet(MessageType.DISCOVER, b"\x00" * 16)))

    def test_leases_survive_restart(self) -> None:
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "leases.json"
            pool = LeasePool("192.168.50.100", "192.168.50.102", 3600, clock=clock, storage=path)
            pool.bind("02-AA-BB-CC-DD-01", "192.168.50.101")
            restored = LeasePool("192.168.50.100", "192.168.50.102", 3600, clock=clock, storage=path)
            self.assertEqual(restored.active()[0].ip, "192.168.50.101")


class DhcpSocketTests(unittest.TestCase):
    def test_exchange_over_real_udp_and_port_is_released(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("127.0.0.1", 0))
            server_port = probe.getsockname()[1]
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.bind(("127.0.0.1", 0))
        client.settimeout(3)
        pool = LeasePool("127.0.0.100", "127.0.0.110", 3600)
        for _cycle in range(3):  # Opakovaný start ověřuje uvolnění portu.
            server = DhcpServer(
                "127.0.0.1", "255.0.0.0", pool, conflict_check=lambda _ip, _mac: False,
                server_port=server_port, client_port=client.getsockname()[1], broadcast_address="127.0.0.1",
            )
            server.start()
            try:
                client.sendto(client_packet(MessageType.DISCOVER).encode(), ("127.0.0.1", server_port))
                reply = DhcpPacket.parse(client.recvfrom(2048)[0])
                self.assertEqual(reply.message_type, MessageType.OFFER)
                self.assertEqual(reply.yiaddr, "127.0.0.100")
                client.sendto(b"garbage", ("127.0.0.1", server_port))
                request = client_packet(
                    MessageType.REQUEST,
                    options={Option.REQUESTED_IP: ip_option(reply.yiaddr), Option.SERVER_ID: ip_option("127.0.0.1")},
                )
                client.sendto(request.encode(), ("127.0.0.1", server_port))
                self.assertEqual(DhcpPacket.parse(client.recvfrom(2048)[0]).message_type, MessageType.ACK)
            finally:
                server.stop()
        client.close()


def adapter(index: int, status: str, description: str = "Microsoft Wi-Fi Direct Virtual Adapter #2",
            ipv4: tuple[am.Ipv4Entry, ...] = (), mac: str = "1A-B5-CD-AC-24-EF") -> am.AdapterInfo:
    return am.AdapterInfo(index, f"if{index}", description, status, mac, medium=9, virtual=True, ipv4=ipv4)


class AdapterTests(unittest.TestCase):
    def test_detects_newly_created_interface(self) -> None:
        self.assertEqual(am.detect_new_virtual_interfaces([], [adapter(5, "Up")])[0].if_index, 5)

    def test_detects_status_transition(self) -> None:
        found = am.detect_new_virtual_interfaces([adapter(5, "Disconnected")], [adapter(5, "Up")])
        self.assertEqual([a.if_index for a in found], [5])

    def test_ignores_already_active_and_other_adapters(self) -> None:
        physical = am.AdapterInfo(12, "Wi-Fi", "Realtek RTL8852BE-VD", "Up", "14-B5-CD-AC-24-EF", medium=9)
        before = [adapter(7, "Up")]
        self.assertEqual(am.detect_new_virtual_interfaces(before, [adapter(7, "Up"), physical]), [])
        self.assertTrue(physical.is_physical_wifi)
        self.assertFalse(adapter(5, "Up").is_physical_wifi)

    def test_link_local_address_is_not_usable(self) -> None:
        item = adapter(5, "Up", ipv4=(am.Ipv4Entry("169.254.1.1", 16), am.Ipv4Entry("192.168.137.1", 24)))
        self.assertEqual([entry.ip for entry in item.usable_ipv4], ["192.168.137.1"])

    def test_virtual_mac_matches_physical(self) -> None:
        self.assertTrue(am.virtual_matches_physical("1A-B5-CD-AC-24-EF", "14:b5:cd:ac:24:ef"))
        self.assertFalse(am.virtual_matches_physical("1A-B5-CD-AC-24-EE", "14-B5-CD-AC-24-EF"))

    def test_neighbor_filter_keeps_only_clients(self) -> None:
        network = ipaddress.ip_network("192.168.137.0/24")
        entries = [
            {"Ip": "192.168.137.50", "Mac": "AA-BB-CC-00-11-22", "State": "Reachable"},
            {"Ip": "192.168.137.1", "Mac": "AA-BB-CC-00-11-23", "State": "Reachable"},
            {"Ip": "192.168.137.255", "Mac": "FF-FF-FF-FF-FF-FF", "State": "Permanent"},
            {"Ip": "224.0.0.22", "Mac": "01-00-5E-00-00-16", "State": "Permanent"},
            {"Ip": "192.168.137.60", "Mac": "00-00-00-00-00-00", "State": "Unreachable"},
        ]
        self.assertEqual([n.ip for n in am.filter_neighbors(entries, network, "192.168.137.1")], ["192.168.137.50"])


class AddressingTests(unittest.TestCase):
    ics = nc.DhcpListener("192.168.137.1", 13580, "svchost", ("SharedAccess",))

    def test_windows_ics_prevents_own_dhcp(self) -> None:
        item = adapter(5, "Up", ipv4=(am.Ipv4Entry("192.168.137.1", 24),))
        plan = nc.decide_addressing(item, [self.ics])
        self.assertIs(plan.mode, nc.AddressingMode.WINDOWS_MANAGED)
        self.assertEqual(plan.host_ip, "192.168.137.1")
        self.assertIn("ICS", plan.listener.description)

    def test_no_dhcp_means_own_server(self) -> None:
        self.assertIs(nc.decide_addressing(adapter(5, "Up"), []).mode, nc.AddressingMode.OWN_DHCP)

    def test_listener_on_other_interface_is_not_used(self) -> None:
        item = adapter(5, "Up", ipv4=(am.Ipv4Entry("169.254.9.9", 16),))
        vmware = nc.DhcpListener("192.168.10.1", 44, "vmnetdhcp")
        self.assertIs(nc.decide_addressing(item, [vmware]).mode, nc.AddressingMode.OWN_DHCP)

    def test_subnet_conflict_ignores_own_interface(self) -> None:
        addresses = [
            {"IfIndex": 5, "Alias": "AP", "Ip": "192.168.50.1", "Prefix": 24},
            {"IfIndex": 12, "Alias": "Wi-Fi", "Ip": "10.0.0.5", "Prefix": 24},
        ]
        self.assertIsNone(nc.find_subnet_conflict(NetworkSettings(), 5, addresses))
        addresses.append({"IfIndex": 16, "Alias": "VMnet8", "Ip": "192.168.50.7", "Prefix": 24})
        self.assertIn("VMnet8", nc.find_subnet_conflict(NetworkSettings(), 5, addresses) or "")


class RecordingRunner:
    def __init__(self, exists: bool = True) -> None:
        self.calls: list[list[str]] = []
        self.exists = exists

    def __call__(self, args: list[str]) -> CommandResult:
        self.calls.append(list(args))
        code = 1 if ("show" in args and not self.exists) else 0
        return CommandResult(tuple(args), code, "", "")


class ChangeTrackingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.state = RuntimeState(Path(self.folder.name) / "state.json")

    def test_physical_adapter_is_never_configured(self) -> None:
        runner = RecordingRunner()
        physical = am.AdapterInfo(12, "Wi-Fi", "Realtek RTL8852BE-VD", "Up", "14-B5-CD-AC-24-EF", medium=9)
        with mock.patch.object(nc, "is_admin", return_value=True):
            with self.assertRaises(nc.NetworkConfigError):
                nc.NetworkConfigurator(self.state, runner).apply_static(physical, NetworkSettings())
        self.assertEqual(runner.calls, [])
        self.assertIsNone(self.state.network)

    def test_static_config_has_no_gateway_and_is_restored(self) -> None:
        runner = RecordingRunner()
        item = am.AdapterInfo(5, "AP", "Microsoft Wi-Fi Direct Virtual Adapter #2", "Up", "1A-B5-CD-AC-24-EF", dhcp_enabled=True)
        configurator = nc.NetworkConfigurator(self.state, runner)
        with mock.patch.object(nc, "is_admin", return_value=True), \
                mock.patch.object(nc, "list_all_ipv4", return_value=[]), \
                mock.patch.object(nc.NetworkConfigurator, "_wait_for_address"), \
                mock.patch.object(nc, "list_adapters", return_value=[item]):
            configurator.apply_static(item, NetworkSettings())
            self.assertEqual(self.state.network["if_index"], 5)
            self.assertIn("gateway=none", runner.calls[0])
            self.assertIn("name=5", runner.calls[0])
            configurator.restore()
        self.assertIn("source=dhcp", runner.calls[-1])
        self.assertIsNone(self.state.network)

    def test_firewall_rule_is_targeted_and_removed(self) -> None:
        runner = RecordingRunner(exists=False)
        manager = fw.FirewallManager(self.state, runner)
        with mock.patch.object(fw, "is_admin", return_value=True):
            manager.add_rule(fw.dhcp_rule())
        command = runner.calls[-1]
        for expected in ("dir=in", "protocol=UDP", "localport=67", "remoteport=68", "interfacetype=wireless"):
            self.assertIn(expected, command)
        self.assertEqual(self.state.firewall_rules, [fw.DHCP_RULE_NAME])
        runner.exists = True
        self.assertEqual(manager.remove_all(), [])
        self.assertIn("delete", runner.calls[-1])
        self.assertEqual(self.state.firewall_rules, [])

    def test_never_touches_foreign_firewall_rules(self) -> None:
        runner = RecordingRunner()
        self.state.add_firewall_rule("Core Networking - DHCP")
        fw.FirewallManager(self.state, runner).remove_all()
        self.assertEqual(runner.calls, [])
        with mock.patch.object(fw, "is_admin", return_value=True):
            with self.assertRaises(fw.FirewallError):
                fw.FirewallManager(self.state, runner).add_rule(fw.FirewallRule("Cizí pravidlo", "UDP", "67"))

    def test_firewall_requires_admin(self) -> None:
        with mock.patch.object(fw, "is_admin", return_value=False):
            with self.assertRaises(fw.FirewallError):
                fw.FirewallManager(self.state, RecordingRunner()).add_rule(fw.dhcp_rule())

    def test_state_survives_restart(self) -> None:
        self.state.add_firewall_rule(fw.DHCP_RULE_NAME)
        self.state.set_network({"if_index": 5})
        restored = RuntimeState(Path(self.folder.name) / "state.json")
        self.assertEqual(restored.firewall_rules, [fw.DHCP_RULE_NAME])
        self.assertEqual(restored.network, {"if_index": 5})


if __name__ == "__main__":
    unittest.main()
