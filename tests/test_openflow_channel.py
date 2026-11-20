"""Chạy mô phỏng qua TCP với một controller giả học MAC kiểu simple_switch_13.

Controller giả dùng đúng codec của ta ở phía server, nên các test này kiểm tra sự ăn khớp
giữa OpenFlowChannel, OpenFlowSession và codec; tính đúng đắn của codec với bên ngoài
được kiểm tra riêng trong test_openflow_crosscheck.py.
"""

import asyncio
import socket
import struct

import pytest

from sdnsim.core.packet import Packet
from sdnsim.core.scheduler import Scheduler
from sdnsim.openflow.channel import OpenFlowChannel
from sdnsim.openflow.codec import (
    OFPBAC_BAD_OUT_PORT, OFPBRC_BAD_TYPE, OFPET_BAD_ACTION, OFPET_BAD_REQUEST,
    OFPET_FLOW_MOD_FAILED, OFPFMFC_UNKNOWN, OFPT_BARRIER_REPLY, OFPT_BARRIER_REQUEST,
    OFPT_ERROR, pack_error, pack_flow_mod, pack_packet_in, pack_packet_out,
    parse_error, parse_packet_in,
)
from sdnsim.openflow.header import (
    Message, OFPT_FEATURES_REPLY, OFPT_FEATURES_REQUEST, OFPT_FLOW_MOD, OFPT_HELLO,
    OFPT_PACKET_IN, pack_message,
)
from sdnsim.openflow.reader import MessageReader
from sdnsim.topology.network import build_network

H1, H2, H3 = "00:00:00:00:00:01", "00:00:00:00:00:02", "00:00:00:00:00:03"
OFPT_MULTIPART_REQUEST = 18

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


class FakeController:
    """Controller OpenFlow 1.3 tối giản: handshake, cài flow table-miss, rồi học MAC."""

    def __init__(self, on_connect=None):
        self.on_connect = on_connect        # hàm(send) chạy sau khi biết dpid của switch
        self.tables = {}                    # dpid -> {mac: cổng}
        self.ready = set()                  # dpid đã hoàn tất handshake
        self.received = []                  # (dpid, Message) mọi thông điệp sau handshake
        self.port = None
        self._server = None
        self._writers = []

    async def start(self):
        self._server = await asyncio.start_server(self._on_connection, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self):
        self._server.close()
        self.disconnect_all()
        await asyncio.wait_for(self._server.wait_closed(), 3)

    def disconnect_all(self):
        for writer in self._writers:
            writer.close()

    async def _on_connection(self, reader, writer):
        self._writers.append(writer)
        parser, pending = MessageReader(), []

        async def recv():
            while not pending:
                data = await reader.read(4096)
                if not data:
                    raise ConnectionError("switch đã đóng kết nối")
                pending.extend(parser.feed(data))
            return pending.pop(0)

        def send(msg):
            writer.write(pack_message(msg))

        try:
            send(Message(OFPT_HELLO, xid=1))
            await recv()                                             # HELLO của switch
            send(Message(OFPT_FEATURES_REQUEST, xid=2))
            features = await recv()
            assert features.type == OFPT_FEATURES_REPLY
            dpid = struct.unpack("!Q", features.body[:8])[0]
            send(pack_flow_mod({}, "CONTROLLER", priority=0))        # table-miss như simple_switch_13
            self.ready.add(dpid)
            if self.on_connect:
                self.on_connect(send)
            while True:
                msg = await recv()
                self.received.append((dpid, msg))
                if msg.type == OFPT_PACKET_IN:
                    self._learn(dpid, msg, send)
        except ConnectionError:
            pass
        finally:
            writer.close()

    def _learn(self, dpid, msg, send):
        in_port, pkt = parse_packet_in(msg)
        table = self.tables.setdefault(dpid, {})
        table[pkt.eth_src] = in_port
        out = table.get(pkt.eth_dst, "FLOOD")
        if out != "FLOOD":
            send(pack_flow_mod(
                {"in_port": in_port, "eth_dst": pkt.eth_dst, "eth_src": pkt.eth_src}, out))
        send(pack_packet_out(out, pkt, in_port=in_port))

    def errors(self):
        return [(dpid, msg) for dpid, msg in self.received if msg.type == OFPT_ERROR]


async def advance(sched, condition, step=0.005, timeout=5.0):
    """Chạy giờ ảo từng bước nhỏ, xen kẽ nhường event loop cho TCP, tới khi điều kiện đúng."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise TimeoutError("điều kiện không đạt trong thời hạn")
        sched.run(until=sched.now + step)
        await asyncio.sleep(0.002)


async def with_network(controller, body, spec=SPEC, down_latency=0.001):
    await controller.start()
    sched = Scheduler()
    channel = OpenFlowChannel(sched, port=controller.port, down_latency=down_latency)
    sched, channel, nodes = build_network(spec, scheduler=sched, channel=channel)
    try:
        await channel.start()
        await advance(sched, lambda: len(controller.ready) == len(channel.switches))
        return await body(sched, channel, nodes)
    finally:
        await channel.stop()
        await controller.stop()


async def ping(sched, nodes, seq):
    h1, h3 = nodes["h1"], nodes["h3"]
    sched.schedule(0, h1.ping, h3.mac, seq, h3.ip)
    await advance(sched, lambda: len(h1.rtts) == seq)


# ------------------------------------------------------------------ đường đi chính

def test_ping_over_tcp_installs_flows_and_rtt_settles():
    async def body(sched, channel, nodes):
        await ping(sched, nodes, 1)
        await ping(sched, nodes, 2)
        before = dict(channel.packet_in_by_switch)
        await ping(sched, nodes, 3)
        return nodes["h1"].rtts, before, dict(channel.packet_in_by_switch)

    rtts, before, after = asyncio.run(with_network(FakeController(), body))

    assert before == {"s1": 3, "s2": 3}           # cùng số PACKET_IN như khi controller trong tiến trình
    assert after == before                        # ping thứ 3 không còn PACKET_IN nào
    # Mỗi lần đi vòng qua controller tốn ít nhất down_latency = 1 ms giờ ảo.
    assert rtts[0] >= 0.010 - 1e-9                # 6 chặng + 4 lần qua controller
    assert rtts[1] >= 0.008 - 1e-9                # 6 chặng + 2 lần qua controller
    assert rtts[2] == pytest.approx(0.006)        # chỉ còn 6 chặng link


def test_down_latency_is_added_for_every_controller_trip():
    async def body(sched, channel, nodes):
        await ping(sched, nodes, 1)
        await ping(sched, nodes, 2)
        await ping(sched, nodes, 3)
        return nodes["h1"].rtts

    rtts = asyncio.run(with_network(FakeController(), body, down_latency=0.05))

    # Ping 1 qua controller 4 lần, ping 2 hai lần, ping 3 không lần nào; mỗi lần cộng 50 ms.
    assert rtts[0] >= 0.006 + 4 * 0.05 - 1e-9
    assert rtts[1] >= 0.006 + 2 * 0.05 - 1e-9
    assert rtts[2] == pytest.approx(0.006)


def test_flows_come_from_controller_with_in_port_and_table_miss_is_ignored():
    async def body(sched, channel, nodes):
        assert nodes["s1"].flows == [] and nodes["s2"].flows == []   # table-miss không thành flow
        await ping(sched, nodes, 1)
        await ping(sched, nodes, 2)
        return nodes

    nodes = asyncio.run(with_network(FakeController(), body))
    for name in ("s1", "s2"):
        assert len(nodes[name].flows) == 2
        assert all({"in_port", "eth_dst", "eth_src"} == set(f["match"]) for f in nodes[name].flows)


def test_each_switch_gets_its_own_dpid():
    controller = FakeController()

    async def body(sched, channel, nodes):
        await ping(sched, nodes, 1)
        return channel.dpids

    dpids = asyncio.run(with_network(controller, body))
    assert dpids == {"s1": 1, "s2": 2}
    assert controller.ready == {1, 2}
    assert set(controller.tables) == {1, 2}


# ----------------------------------------------- thông điệp bị từ chối: ERROR, giữ phiên

def _delete_flow_mod():
    good = pack_flow_mod({"eth_dst": H3}, 3, xid=77)
    body = bytearray(good.body)
    body[17] = 3                                   # command = DELETE
    return Message(OFPT_FLOW_MOD, xid=77, body=bytes(body))


def _packet_out_to_missing_port():
    pkt = Packet(H1, H3, "icmp_req", seq=1, ip_src="10.0.0.1", ip_dst="10.0.0.3")
    return pack_packet_out(99, pkt, in_port=1, xid=78)


def _flow_mod_to_missing_port():
    return pack_flow_mod({"eth_dst": H3}, 99, xid=79)


def _unsupported_message():
    return Message(OFPT_MULTIPART_REQUEST, xid=80, body=b"\x00" * 8)


@pytest.mark.parametrize("make_bad, expected", [
    (_delete_flow_mod, (OFPET_FLOW_MOD_FAILED, OFPFMFC_UNKNOWN)),
    (_packet_out_to_missing_port, (OFPET_BAD_ACTION, OFPBAC_BAD_OUT_PORT)),
    (_flow_mod_to_missing_port, (OFPET_BAD_ACTION, OFPBAC_BAD_OUT_PORT)),
    (_unsupported_message, (OFPET_BAD_REQUEST, OFPBRC_BAD_TYPE)),
])
def test_rejected_message_gets_error_and_session_survives(make_bad, expected):
    bad = make_bad()
    controller = FakeController(on_connect=lambda send: send(bad))

    async def body(sched, channel, nodes):
        await advance(sched, lambda: len(controller.errors()) == 2)   # mỗi switch báo một lỗi
        await ping(sched, nodes, 1)                                   # phiên vẫn dùng được
        return nodes["h1"].rtts, channel.errors

    rtts, channel_errors = asyncio.run(with_network(controller, body))

    assert len(rtts) == 1
    for _dpid, err in controller.errors():
        assert err.xid == bad.xid                                     # ERROR mang xid của yêu cầu
        err_type, code, data = parse_error(err)
        assert (err_type, code) == expected
        assert data == pack_message(bad)[:64]
    assert len(channel_errors) == 2


def test_barrier_request_is_answered_after_earlier_messages():
    barrier = Message(OFPT_BARRIER_REQUEST, xid=81)
    controller = FakeController(on_connect=lambda send: send(barrier))

    async def body(sched, channel, nodes):
        def replies():
            return [m for _, m in controller.received if m.type == OFPT_BARRIER_REPLY]
        await advance(sched, lambda: len(replies()) == 2)
        return replies()

    replies = asyncio.run(with_network(controller, body))
    assert [m.xid for m in replies] == [81, 81]


# ------------------------------------------------------------------ mất kết nối

def test_packet_in_is_dropped_without_crash_when_not_connected():
    sched = Scheduler()
    channel = OpenFlowChannel(sched)                    # chưa gọi start()
    sched, channel, nodes = build_network(SPEC, scheduler=sched, channel=channel)

    sched.schedule(0, nodes["h1"].ping, H3, 1, "10.0.0.3")
    sched.run()

    assert channel.packet_in_count == 1                 # request bị table-miss tại s1 rồi bị bỏ
    assert nodes["h1"].rtts == []
    assert not channel.connected("s1")


def test_controller_disconnect_is_detected_and_traffic_is_dropped():
    controller = FakeController()

    async def body(sched, channel, nodes):
        controller.disconnect_all()
        await advance(sched, lambda: not channel.connected("s1") and not channel.connected("s2"))
        sched.schedule(0, nodes["h1"].ping, H3, 1, "10.0.0.3")
        sched.run(until=sched.now + 0.1)
        return nodes["h1"].rtts

    assert asyncio.run(with_network(controller, body)) == []


def test_start_fails_cleanly_when_no_controller_is_listening():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed_port = s.getsockname()[1]                # cổng vừa giải phóng, không ai nghe

    async def run():
        sched = Scheduler()
        channel = OpenFlowChannel(sched, port=closed_port)
        build_network(SPEC, scheduler=sched, channel=channel)
        with pytest.raises(OSError):
            await channel.start()
        assert not channel.connected("s1")

    asyncio.run(run())


# ------------------------------------------------ PACKET_IN thật gửi đi đúng nội dung

def test_packet_in_sent_to_controller_matches_codec_output():
    controller = FakeController()

    async def body(sched, channel, nodes):
        sched.schedule(0, nodes["h1"].ping, H3, 5, "10.0.0.3")
        await advance(sched, lambda: any(m.type == OFPT_PACKET_IN for _, m in controller.received))
        return next(m for _, m in controller.received if m.type == OFPT_PACKET_IN)

    msg = asyncio.run(with_network(controller, body))
    in_port, pkt = parse_packet_in(msg)
    assert in_port == 1                                  # h1 nối vào cổng 1 của s1
    assert (pkt.eth_src, pkt.eth_dst, pkt.kind, pkt.seq) == (H1, H3, "icmp_req", 5)
    assert msg.body == pack_packet_in(1, pkt, xid=msg.xid).body