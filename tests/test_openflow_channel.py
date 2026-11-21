"""Chạy mô phỏng qua TCP với một controller giả học MAC kiểu simple_switch_13.

Controller giả dùng đúng codec của ta ở phía server, nên các test này kiểm tra sự ăn khớp
giữa OpenFlowChannel, OpenFlowSession và codec; tính đúng đắn của codec với bên ngoài
được kiểm tra riêng trong test_openflow_crosscheck.py.
"""

import asyncio
import logging
import socket
import struct

import pytest

from openflow_support import FakeController, advance
from sdnsim.core.packet import Packet
from sdnsim.core.scheduler import Scheduler
from sdnsim.openflow.channel import OpenFlowChannel
from sdnsim.openflow.codec import (
    MULTIPART_HEADER, OFPBAC_BAD_OUT_PORT, OFPBRC_BAD_LEN, OFPBRC_BAD_MULTIPART,
    OFPBRC_BAD_TYPE, OFPET_BAD_ACTION, OFPET_BAD_REQUEST, OFPET_FLOW_MOD_FAILED,
    OFPFMFC_UNKNOWN, OFPMP_PORT_DESC, OFPT_BARRIER_REPLY, OFPT_BARRIER_REQUEST,
    OFPT_MULTIPART_REPLY, OFPT_MULTIPART_REQUEST, PORT_DESC_ENTRY,
    pack_flow_mod, pack_packet_in, pack_packet_out, parse_error, parse_packet_in,
)
from sdnsim.openflow.header import Message, OFPT_FLOW_MOD, OFPT_PACKET_IN, pack_message
from sdnsim.topology.network import build_network

H1, H2, H3 = "00:00:00:00:00:01", "00:00:00:00:00:02", "00:00:00:00:00:03"

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


def test_idle_timeout_set_by_controller_expires_flows_in_simulated_time():
    async def body(sched, channel, nodes):
        await ping(sched, nodes, 1)
        timeouts = [f["idle_timeout"] for name in ("s1", "s2") for f in nodes[name].flows]
        # Chạy giờ ảo (không có gói nào) cho tới khi mọi flow hết hạn.
        await advance(sched, lambda: not nodes["s1"].flows and not nodes["s2"].flows, step=0.05)
        before = channel.packet_in_count
        await ping(sched, nodes, 2)
        return timeouts, before, channel.packet_in_count, nodes

    timeouts, before, after, nodes = asyncio.run(
        with_network(FakeController(idle_timeout=1), body))

    assert timeouts == [1.0, 1.0]                  # sau ping 1 mỗi switch có một flow (chiều trả lời), mang idle_timeout của FLOW_MOD
    assert after > before                          # flow đã hết hạn nên ping 2 phải hỏi lại controller
    assert len(nodes["h1"].rtts) == 2              # nhưng ping vẫn thành công
    assert nodes["s1"].flows and all(f["idle_timeout"] == 1.0 for f in nodes["s1"].flows)   # flow được cài lại


@pytest.mark.parametrize("controller_timeout, expected", [(2, "idle_timeout=2.0"), (None, "idle_timeout=None")])
def test_flow_mod_is_logged_with_its_idle_timeout(caplog, controller_timeout, expected):
    caplog.set_level(logging.INFO, logger="sdnsim.openflow")

    async def body(sched, channel, nodes):
        await ping(sched, nodes, 1)

    asyncio.run(with_network(FakeController(idle_timeout=controller_timeout), body))
    messages = [r.getMessage() for r in caplog.records if r.name == "sdnsim.openflow"]
    assert any("FLOW_MOD" in m and expected in m for m in messages)


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
    return Message(7, xid=80)                          # GET_CONFIG_REQUEST: chưa hỗ trợ


def _unsupported_multipart():
    return Message(OFPT_MULTIPART_REQUEST, xid=82, body=struct.pack("!HH4x", 0, 0))   # DESC


def _truncated_multipart():
    return Message(OFPT_MULTIPART_REQUEST, xid=84, body=b"\x00")


@pytest.mark.parametrize("make_bad, expected", [
    (_delete_flow_mod, (OFPET_FLOW_MOD_FAILED, OFPFMFC_UNKNOWN)),
    (_packet_out_to_missing_port, (OFPET_BAD_ACTION, OFPBAC_BAD_OUT_PORT)),
    (_flow_mod_to_missing_port, (OFPET_BAD_ACTION, OFPBAC_BAD_OUT_PORT)),
    (_unsupported_message, (OFPET_BAD_REQUEST, OFPBRC_BAD_TYPE)),
    (_unsupported_multipart, (OFPET_BAD_REQUEST, OFPBRC_BAD_MULTIPART)),
    (_truncated_multipart, (OFPET_BAD_REQUEST, OFPBRC_BAD_LEN)),
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


def test_port_desc_request_is_answered_with_the_switch_ports():
    request = Message(OFPT_MULTIPART_REQUEST, xid=83,
                      body=MULTIPART_HEADER.pack(OFPMP_PORT_DESC, 0))
    controller = FakeController(on_connect=lambda send: send(request))

    async def body(sched, channel, nodes):
        def replies():
            return {dpid: m for dpid, m in controller.received if m.type == OFPT_MULTIPART_REPLY}
        await advance(sched, lambda: len(replies()) == 2)
        return replies()

    replies = asyncio.run(with_network(controller, body))

    def decode(msg):
        mp_type, flags = MULTIPART_HEADER.unpack_from(msg.body)
        entries = [PORT_DESC_ENTRY.unpack_from(msg.body, off)
                   for off in range(MULTIPART_HEADER.size, len(msg.body), PORT_DESC_ENTRY.size)]
        return mp_type, flags, [(e[0], e[2].rstrip(b"\x00").decode()) for e in entries]

    assert replies[1].xid == replies[2].xid == 83
    assert decode(replies[1]) == (OFPMP_PORT_DESC, 0, [(1, "s1-eth1"), (2, "s1-eth2"), (3, "s1-eth3")])
    assert decode(replies[2]) == (OFPMP_PORT_DESC, 0, [(1, "s2-eth1"), (2, "s2-eth2")])


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