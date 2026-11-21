import pytest

from sdnsim.control.channel import Channel
from sdnsim.core.messages import FlowMod, PacketOut
from sdnsim.core.packet import Packet
from sdnsim.core.scheduler import Scheduler
from sdnsim.core.switch import Switch
from sdnsim.topology.network import build_network

H1, H2, H3 = "00:00:00:00:00:01", "00:00:00:00:00:02", "00:00:00:00:00:03"


class CapturePort:
    def __init__(self):
        self.packets = []

    def transmit(self, sender, pkt):
        self.packets.append(pkt)


def make_switch():
    sched = Scheduler()
    channel = Channel(sched)
    channel.controller = type("C", (), {"on_packet_in": lambda self, msg: None})()
    sw = Switch(sched, "s1", channel)
    ports = {p: CapturePort() for p in (1, 2, 3)}
    sw.ports.update(ports)
    return sw, channel, ports


def pkt_to_h2():
    return Packet(H1, H2, "icmp_req", seq=1, ip_src="10.0.0.1", ip_dst="10.0.0.2")


def test_in_port_condition_matches_only_that_port():
    sw, channel, ports = make_switch()
    sw.flow_mod({"in_port": 1, "eth_dst": H2}, 2)

    sw.receive(1, pkt_to_h2())
    assert len(ports[2].packets) == 1
    assert channel.packet_in_count == 0

    sw.receive(3, pkt_to_h2())             # cùng gói nhưng đến từ cổng 3
    assert len(ports[2].packets) == 1      # không được chuyển tiếp
    assert channel.packet_in_count == 1    # table-miss


def test_flow_without_in_port_still_matches_any_port():
    sw, channel, ports = make_switch()
    sw.flow_mod({"eth_dst": H2}, 2)
    sw.receive(1, pkt_to_h2())
    sw.receive(3, pkt_to_h2())
    assert len(ports[2].packets) == 2
    assert channel.packet_in_count == 0


def test_priority_decides_between_in_port_and_generic_flow():
    sw, channel, ports = make_switch()
    sw.flow_mod({"eth_dst": H2}, 3, priority=1)
    sw.flow_mod({"in_port": 1, "eth_dst": H2}, 2, priority=10)

    sw.receive(1, pkt_to_h2())             # khớp cả hai, ưu tiên cao thắng
    sw.receive(2, pkt_to_h2())             # chỉ khớp flow chung
    assert len(ports[2].packets) == 1
    assert len(ports[3].packets) == 1


def test_empty_match_flow_matches_everything():
    sw, channel, ports = make_switch()
    sw.flow_mod({}, 3, priority=0)
    sw.receive(1, pkt_to_h2())
    assert len(ports[3].packets) == 1


def test_handle_installs_flow_with_in_port_from_message():
    sw, channel, ports = make_switch()
    channel.send(FlowMod("s1", {"in_port": 2, "eth_dst": H2}, 3))
    assert sw.flows[0]["match"] == {"in_port": 2, "eth_dst": H2}
    sw.receive(2, pkt_to_h2())
    assert len(ports[3].packets) == 1


def test_flow_mod_with_same_match_and_priority_replaces_the_old_flow():
    sw, channel, ports = make_switch()
    sw.flow_mod({"eth_dst": H2}, 2)
    sw.receive(1, pkt_to_h2())                       # flow cũ đã đếm 1 gói
    sw.flow_mod({"eth_dst": H2}, 3)                  # cùng match và priority: thay thế, không nhân đôi
    assert len(sw.flows) == 1
    assert sw.flows[0]["out_port"] == 3 and sw.flows[0]["packets"] == 0
    sw.receive(1, pkt_to_h2())
    assert len(ports[3].packets) == 1 and len(ports[2].packets) == 1


def test_flows_with_different_match_or_priority_coexist():
    sw, channel, ports = make_switch()
    sw.flow_mod({"eth_dst": H2}, 2, priority=1)
    sw.flow_mod({"eth_dst": H2}, 3, priority=2)      # khác priority
    sw.flow_mod({"eth_dst": H3}, 3, priority=1)      # khác match
    assert len(sw.flows) == 3


SPEC = {
    "hosts": [
        {"name": "h1", "mac": H1, "ip": "10.0.0.1"},
        {"name": "h2", "mac": H2, "ip": "10.0.0.2"},
        {"name": "h3", "mac": H3, "ip": "10.0.0.3"},
    ],
    "switches": [{"name": "s1"}, {"name": "s2"}],
    "links": [
        {"a": "h1", "a_port": 1, "b": "s1", "b_port": 1},
        {"a": "h2", "a_port": 1, "b": "s1", "b_port": 2},
        {"a": "s1", "a_port": 3, "b": "s2", "b_port": 1},
        {"a": "h3", "a_port": 1, "b": "s2", "b_port": 2},
    ],
}


class PortAwareController:
    """Học MAC như simple_switch_13 của Ryu: flow match cả in_port, eth_dst và eth_src."""

    def __init__(self):
        self.table = {}
        self.channel = None

    def on_packet_in(self, msg):
        t = self.table.setdefault(msg.switch, {})
        t[msg.pkt.eth_src] = msg.in_port
        out = t.get(msg.pkt.eth_dst, "FLOOD")
        if out != "FLOOD":
            self.channel.send(FlowMod(
                msg.switch,
                {"in_port": msg.in_port, "eth_dst": msg.pkt.eth_dst, "eth_src": msg.pkt.eth_src},
                out))
        self.channel.send(PacketOut(msg.switch, msg.pkt, out, msg.in_port))


def test_port_aware_controller_gives_same_results_over_two_switches():
    sched, ch, nodes = build_network(SPEC, controller=PortAwareController())
    h1, h3 = nodes["h1"], nodes["h3"]
    for seq in (1, 2, 3):
        sched.schedule(0.1, h1.ping, h3.mac, seq, h3.ip)
        sched.run()

    assert h1.rtts == [pytest.approx(0.010), pytest.approx(0.008), pytest.approx(0.006)]
    assert ch.packet_in_by_switch == {"s1": 3, "s2": 3}
    assert all("in_port" in f["match"] for f in nodes["s1"].flows)