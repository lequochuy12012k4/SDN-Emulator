"""Thống kê cổng và flow qua OpenFlow: mã hóa/giải mã và trả lời của switch mô phỏng."""

import asyncio
import copy
import struct

import pytest

from openflow_support import FakeController, advance
from sdnsim.core.packet import Packet
from sdnsim.core.scheduler import Scheduler
from sdnsim.openflow.channel import OpenFlowChannel
from sdnsim.openflow.codec import (
    MAX_MESSAGE_BYTES, MULTIPART_HEADER, OFPBMC_BAD_FIELD, OFPBRC_BAD_PORT, OFPET_BAD_MATCH,
    OFPET_BAD_REQUEST, OFPMP_AGGREGATE, OFPMP_DESC, OFPMP_FLOW, OFPMP_GROUP,
    OFPMP_METER, OFPMP_GROUP_DESC, OFPMP_METER_CONFIG, OFPMP_PORT_STATS,
    OFPMP_TABLE, OFPMPF_REPLY_MORE, OFPP_ANY, OFPGC_ADD, OFPGC_DELETE,
    OFPGC_MODIFY, OFPGT_ALL, OFPGT_INDIRECT, OFPMF_BURST, OFPMF_KBPS,
    OFPMF_STATS, OFPMC_ADD, OFPMC_DELETE, OFPMC_MODIFY, OFPGMFC_GROUP_IN_USE,
    OFPGMFC_BAD_TYPE, OFPMMFC_METER_IN_USE, OFPMMFC_UNSUPPORTED,
    OFPET_GROUP_MOD_FAILED, OFPET_METER_MOD_FAILED,
    OFPT_ERROR, OFPT_MULTIPART_REPLY, OFPT_MULTIPART_REQUEST, OFPTT_ALL,
    DESC_STATS, PORT_STATS_ENTRY, TABLE_STATS_ENTRY, BucketStats, FlowStats,
    FlowStatsRequest, GroupBucket, GroupDesc, GroupStats, MeterConfig, MeterStats, PortStats,
    pack_aggregate_stats_reply, pack_aggregate_stats_request, pack_flow_stats_reply,
    pack_flow_stats_request,
    pack_flow_mod, pack_group_mod, pack_group_stats_request, pack_group_desc_request,
    pack_meter_mod, pack_meter_stats_request, pack_meter_config_request,
    pack_port_stats_reply, pack_port_stats_request, parse_aggregate_stats_reply,
    parse_error, parse_flow_stats_reply, parse_flow_stats_request,
    parse_group_desc_reply, parse_group_stats_reply, parse_meter_config_reply,
    parse_meter_stats_reply, parse_multipart_reply, parse_port_stats_reply,
    parse_port_stats_request,
)
from sdnsim.openflow.header import HEADER_LEN, Message, OFPT_PACKET_IN, pack_message
from sdnsim.topology.network import build_network
from test_openflow_channel import SPEC, with_network, ping

H1, H3 = "00:00:00:00:00:01", "00:00:00:00:00:03"


# ------------------------------------------------------------- mã hóa / giải mã

def test_entry_sizes_match_the_spec():
    assert PORT_STATS_ENTRY.size == 112


def test_port_stats_reply_golden_bytes():
    stats = PortStats(3, rx_packets=1, tx_packets=2, rx_bytes=50, tx_bytes=100, tx_dropped=7,
                      duration_sec=9, duration_nsec=500)
    [msg] = pack_port_stats_reply([stats], xid=5)
    assert (msg.type, msg.xid) == (OFPT_MULTIPART_REPLY, 5)
    assert msg.body[:8] == bytes.fromhex("0004" "0000" "00000000")       # PORT_STATS, không còn phần nào
    entry = msg.body[8:]
    assert len(entry) == 112
    assert entry[:4] == bytes.fromhex("00000003")                          # port_no
    q = lambda i: struct.unpack_from("!Q", entry, 8 + 8 * i)[0]            # noqa: E731
    assert [q(i) for i in range(6)] == [1, 2, 50, 100, 0, 7]               # rx/tx packets, rx/tx bytes, rx/tx dropped
    assert struct.unpack_from("!II", entry, 104) == (9, 500)               # duration


@pytest.mark.parametrize("entries", [
    [], [PortStats(1)], [PortStats(1, 5, 6, 7, 8, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10), PortStats(2)],
])
def test_port_stats_roundtrip(entries):
    [msg] = pack_port_stats_reply(entries, xid=1)
    assert parse_port_stats_reply(msg) == entries


def test_flow_stats_entry_golden_bytes():
    flow = FlowStats({"eth_dst": H3}, 3, 1, 5.0, 4, 200, 2, 500)
    [msg] = pack_flow_stats_reply([flow], xid=6)
    entry = msg.body[8:]
    assert len(entry) == 48 + 16 + 24                                      # cố định + match + instruction
    assert struct.unpack_from("!HBxII", entry) == (88, 0, 2, 500)         # length, table, duration
    assert struct.unpack_from("!HHHH", entry, 12) == (1, 5, 0, 0)         # priority, idle, hard, flags
    assert struct.unpack_from("!QQQ", entry, 24) == (0, 4, 200)           # cookie, packets, bytes
    assert entry[48:64] == bytes.fromhex("0001000e" "80000606" "000000000003" "0000")   # match
    assert entry[64:] == bytes.fromhex("0004" "0018" "00000000"
                                       "0000" "0010" "00000003" "ffe5" "000000000000")  # instruction


@pytest.mark.parametrize("flows", [
    [],
    [FlowStats({}, "CONTROLLER", 0, None, 0, 0)],
    [FlowStats({"in_port": 1, "eth_dst": H3, "eth_src": H1}, 3, 1, 5.0, 4, 200, 2, 500),
     FlowStats({"eth_dst": H1}, "FLOOD", 10, None, 0, 0, 0, 0)],
])
def test_flow_stats_roundtrip(flows):
    [msg] = pack_flow_stats_reply(flows, xid=1)
    assert parse_flow_stats_reply(msg) == flows


def test_large_reply_is_split_with_reply_more_flags():
    entries = [PortStats(1 + i % 1000, rx_packets=i) for i in range(1000)]
    messages = pack_port_stats_reply(entries, xid=42)
    assert len(messages) == 2
    assert all(len(pack_message(m)) <= MAX_MESSAGE_BYTES and m.xid == 42 for m in messages)
    flags = [parse_multipart_reply(m)[1] for m in messages]
    assert flags == [OFPMPF_REPLY_MORE, 0]                                 # chỉ thông điệp cuối là hết
    assert [s for m in messages for s in parse_port_stats_reply(m)] == entries


def test_empty_reply_is_still_one_message():
    [msg] = pack_flow_stats_reply([], xid=3)
    assert parse_multipart_reply(msg) == (OFPMP_FLOW, 0, b"")


def test_requests_roundtrip():
    request = pack_port_stats_request(2, xid=1)
    assert parse_port_stats_request(request.body[MULTIPART_HEADER.size:]) == 2
    assert parse_port_stats_request(pack_port_stats_request().body[8:]) == OFPP_ANY

    wanted = FlowStatsRequest(0, 3, {"eth_dst": H3, "in_port": 1}, 0xA0, 0xF0, 7)
    assert parse_flow_stats_request(pack_flow_stats_request(wanted).body[8:]) == wanted
    assert parse_flow_stats_request(pack_flow_stats_request().body[8:]) == FlowStatsRequest(
        OFPTT_ALL, None, {})


def test_parsers_reject_bad_input():
    with pytest.raises(ValueError):
        parse_port_stats_request(b"\x00\x00")
    with pytest.raises(ValueError):
        parse_flow_stats_request(b"\x00" * 10)
    with pytest.raises(ValueError):
        parse_port_stats_reply(Message(OFPT_PACKET_IN, body=bytes(8)))
    [flow_reply] = pack_flow_stats_reply([FlowStats({}, 1, 1, None, 0, 0)])
    with pytest.raises(ValueError):                                         # sai loại multipart
        parse_port_stats_reply(flow_reply)
    [port_reply] = pack_port_stats_reply([PortStats(1)])
    with pytest.raises(ValueError):
        parse_port_stats_reply(Message(OFPT_MULTIPART_REPLY, body=port_reply.body[:-1]))
    with pytest.raises(ValueError):
        parse_flow_stats_reply(Message(OFPT_MULTIPART_REPLY, body=flow_reply.body[:-3]))


# ------------------------------------------------- switch mô phỏng trả lời qua TCP

async def ask(controller, sched, dpid, request):
    """Gửi một yêu cầu tới switch `dpid`, chạy giờ ảo cho tới khi nhận đủ trả lời (hoặc ERROR)."""
    controller.senders[dpid](request)

    def answers():
        return [m for d, m in controller.received
                if d == dpid and m.xid == request.xid and m.type in (OFPT_MULTIPART_REPLY, OFPT_ERROR)]

    def finished():
        got = answers()
        return bool(got) and (got[-1].type == OFPT_ERROR or parse_multipart_reply(got[-1])[1] == 0)

    await advance(sched, finished)
    return answers()


def with_xid(msg, xid):
    return Message(msg.type, xid=xid, body=msg.body)


def test_port_stats_match_the_switch_counters():
    controller = FakeController()

    async def body(sched, channel, nodes):
        await ping(sched, nodes, 1)
        replies = await ask(controller, sched, 1, with_xid(pack_port_stats_request(), 90))
        return replies, nodes["s1"], sched.now

    replies, s1, now = asyncio.run(with_network(controller, body))
    stats = {s.port_no: s for m in replies for s in parse_port_stats_reply(m)}

    assert set(stats) == {1, 2, 3}                                          # mọi cổng đã nối của s1
    for port, s in stats.items():
        c = s1.port_stats[port]
        assert (s.rx_packets, s.tx_packets, s.rx_bytes, s.tx_bytes) == (
            c["rx_packets"], c["tx_packets"], c["rx_bytes"], c["tx_bytes"])
    assert stats[1].rx_packets > 0 and stats[1].rx_bytes == 50 * stats[1].rx_packets
    assert stats[1].duration_sec == int(now) or stats[1].duration_sec == int(now) - 1


def test_port_stats_for_one_port_and_for_a_missing_port():
    controller = FakeController()

    async def body(sched, channel, nodes):
        await ping(sched, nodes, 1)
        one = await ask(controller, sched, 1, with_xid(pack_port_stats_request(3), 91))
        missing = await ask(controller, sched, 1, with_xid(pack_port_stats_request(99), 92))
        await ping(sched, nodes, 2)                                          # phiên vẫn dùng được
        return one, missing

    one, missing = asyncio.run(with_network(controller, body))
    assert [s.port_no for m in one for s in parse_port_stats_reply(m)] == [3]
    [error] = missing
    assert parse_error(error)[:2] == (OFPET_BAD_REQUEST, OFPBRC_BAD_PORT)


def test_tx_dropped_counts_packets_dropped_by_the_links_queue():
    spec = copy.deepcopy(SPEC)
    spec["links"][2].update({"bandwidth": 10_000, "queue_limit": 1})        # link s1-s2: 40 ms mỗi gói
    controller = FakeController()

    async def body(sched, channel, nodes):
        for seq in range(1, 11):
            nodes["h1"].ping_ip("10.0.0.3", seq)
        await advance(sched, lambda: sched.now > 3.0, step=0.05)
        replies = await ask(controller, sched, 1, with_xid(pack_port_stats_request(3), 93))
        return replies, nodes["s1"].ports[3].stats[nodes["s1"]]["dropped_queue"]

    replies, link_drops = asyncio.run(with_network(controller, body, spec=spec))
    [stats] = [s for m in replies for s in parse_port_stats_reply(m)]
    assert link_drops > 0                                                    # tắc nghẽn có thật
    assert stats.tx_dropped == link_drops


def flow_rows(flows):
    return sorted((tuple(sorted(f.match.items())), f.out_port, f.priority, f.idle_timeout,
                   f.packet_count, f.byte_count) for f in flows)


def test_flow_stats_match_the_switch_flow_table():
    controller = FakeController(idle_timeout=5)

    async def body(sched, channel, nodes):
        await ping(sched, nodes, 1)
        await ping(sched, nodes, 2)
        replies = await ask(controller, sched, 1, with_xid(pack_flow_stats_request(), 94))
        return replies, nodes["s1"]

    replies, s1 = asyncio.run(with_network(controller, body))
    reported = [f for m in replies for f in parse_flow_stats_reply(m)]
    expected = [FlowStats(dict(f["match"]), f["out_port"], f["priority"], f["idle_timeout"],
                          f["packets"], f["bytes"]) for f in s1.flows]
    assert len(reported) == len(s1.flows) >= 2
    assert flow_rows(reported) == flow_rows(expected)
    assert all(f.idle_timeout == 5.0 for f in reported)
    assert any(f.packet_count > 0 for f in reported)                         # bộ đếm có số thật


def test_flow_stats_filters_by_match_out_port_and_table():
    controller = FakeController()

    async def body(sched, channel, nodes):
        await ping(sched, nodes, 1)
        await ping(sched, nodes, 2)

        async def flows_for(xid, request):
            replies = await ask(controller, sched, 1, with_xid(request, xid))
            return [f for m in replies for f in parse_flow_stats_reply(m)]

        exact_match = dict(nodes["s1"].flows[0]["match"])
        return {
            "all": await flows_for(95, pack_flow_stats_request()),
            "by_match": await flows_for(
                96, pack_flow_stats_request(FlowStatsRequest(OFPTT_ALL, None, exact_match))),
            "partial_match": await flows_for(
                100, pack_flow_stats_request(FlowStatsRequest(OFPTT_ALL, None, {"eth_dst": H3}))),
            "by_port": await flows_for(97, pack_flow_stats_request(FlowStatsRequest(0, 3, {}))),
            "by_group": await flows_for(
                108, pack_flow_stats_request(FlowStatsRequest(0, None, {}, out_group=7))),
            "table1": await flows_for(98, pack_flow_stats_request(FlowStatsRequest(1, None, {}))),
            "none": await flows_for(99, pack_flow_stats_request(FlowStatsRequest(0, None, {"eth_dst": "00:00:00:00:00:99"}))),
        }

    got = asyncio.run(with_network(controller, body))
    assert len(got["all"]) >= 2
    assert len(got["by_match"]) == 1
    assert got["by_match"][0].match == got["all"][0].match
    assert got["partial_match"]
    assert all(f.match["eth_dst"] == H3 for f in got["partial_match"])
    assert got["by_port"] and all(f.out_port == 3 for f in got["by_port"])
    assert got["table1"] == [] and got["none"] == [] and got["by_group"] == []


def test_flow_stats_filters_cookie_and_preserves_flow_cookie():
    def install_flows(send):
        send(pack_flow_mod({"eth_dst": H3}, 3, cookie=0xA1))
        send(pack_flow_mod({"eth_dst": H1}, 1, cookie=0xA2))

    controller = FakeController(on_connect=install_flows)

    async def body(sched, channel, nodes):
        await advance(sched, lambda: len(nodes["s1"].flows) == 2)
        request = FlowStatsRequest(0, None, {"eth_dst": H3}, 0xA1, 0xFF)
        replies = await ask(controller, sched, 1, with_xid(pack_flow_stats_request(request), 104))
        return [f for m in replies for f in parse_flow_stats_reply(m)]

    flows = asyncio.run(with_network(controller, body))
    assert len(flows) == 1
    assert flows[0].cookie == 0xA1


def test_aggregate_stats_uses_the_flow_filters():
    controller = FakeController()

    async def body(sched, channel, nodes):
        await ping(sched, nodes, 1)
        request = pack_aggregate_stats_request(xid=101)
        replies = await ask(controller, sched, 1, request)
        return parse_aggregate_stats_reply(replies[0]), nodes["s1"].flows

    aggregate, flows = asyncio.run(with_network(controller, body))
    assert aggregate.flow_count == len(flows)
    assert aggregate.packet_count == sum(f["packets"] for f in flows)
    assert aggregate.byte_count == sum(f["bytes"] for f in flows)


def test_group_and_meter_stats_requests_return_empty_lists():
    controller = FakeController()

    async def body(sched, channel, nodes):
        group = await ask(controller, sched, 1, pack_group_stats_request(xid=102))
        meter = await ask(controller, sched, 1, pack_meter_stats_request(xid=103))
        return group, meter

    group, meter = asyncio.run(with_network(controller, body))
    assert parse_group_stats_reply(group[0]) == []
    assert parse_meter_stats_reply(meter[0]) == []


def test_group_and_meter_mods_drive_dataplane_and_report_live_stats():
    controller = FakeController()

    async def body(sched, channel, nodes):
        switch = nodes["s1"]
        send = controller.senders[1]
        flags = OFPMF_KBPS | OFPMF_BURST | OFPMF_STATS
        send(pack_group_mod(OFPGC_ADD, OFPGT_ALL, 7, [
            GroupBucket(1), GroupBucket(2),
        ]))
        send(pack_meter_mod(OFPMC_ADD, flags, 8, rate=1, burst_size=1))
        send(pack_flow_mod({}, None, priority=10, group_id=7, meter_id=8))
        await advance(sched, lambda: (
            7 in switch.groups and 8 in switch.meters
            and any(flow.get("group_id") == 7 for flow in switch.flows)))

        for seq in range(1, 4):
            switch.receive(1, Packet(H1, H3, "icmp_req", seq=seq))
        await advance(sched, lambda: switch.meters[8]["packets"] == 3)

        group_stats = await ask(controller, sched, 1, pack_group_stats_request(xid=110))
        group_desc = await ask(controller, sched, 1, pack_group_desc_request(xid=111))
        meter_stats = await ask(controller, sched, 1, pack_meter_stats_request(xid=112))
        meter_config = await ask(controller, sched, 1, pack_meter_config_request(xid=113))
        flows = await ask(controller, sched, 1, with_xid(pack_flow_stats_request(), 114))
        blocked_group_delete = await ask(
            controller, sched, 1, pack_group_mod(OFPGC_DELETE, OFPGT_ALL, 7, xid=115))
        blocked_meter_delete = await ask(
            controller, sched, 1, pack_meter_mod(OFPMC_DELETE, 0, 8, xid=116))

        send(pack_group_mod(OFPGC_ADD, OFPGT_INDIRECT, 10, [GroupBucket(2)]))
        send(pack_meter_mod(OFPMC_ADD, flags, 11, rate=100, burst_size=10))
        await advance(sched, lambda: 10 in switch.groups and 11 in switch.meters)
        send(pack_group_mod(OFPGC_MODIFY, OFPGT_INDIRECT, 10, [GroupBucket(3)]))
        send(pack_meter_mod(OFPMC_MODIFY, flags, 11, rate=200, burst_size=20))
        await advance(sched, lambda: (
            switch.groups[10]["buckets"][0]["out_port"] == 3
            and switch.meters[11]["rate"] == 200))
        send(pack_group_mod(OFPGC_DELETE, OFPGT_INDIRECT, 10))
        send(pack_meter_mod(OFPMC_DELETE, 0, 11))
        await advance(sched, lambda: 10 not in switch.groups and 11 not in switch.meters)
        return (switch, group_stats, group_desc, meter_stats, meter_config, flows,
                blocked_group_delete, blocked_meter_delete)

    (switch, group_stats, group_desc, meter_stats, meter_config, flows,
     blocked_group_delete, blocked_meter_delete) = asyncio.run(with_network(controller, body))

    [group] = parse_group_stats_reply(group_stats[0])
    assert (group.group_id, group.ref_count, group.packet_count, group.byte_count) == (
        7, 1, 2, 100)
    assert group.bucket_stats == [BucketStats(2, 100), BucketStats(2, 100)]
    assert parse_group_desc_reply(group_desc[0]) == [
        GroupDesc(OFPGT_ALL, 7, [GroupBucket(1), GroupBucket(2)])]

    [meter] = parse_meter_stats_reply(meter_stats[0])
    assert (meter.meter_id, meter.flow_count, meter.packet_in_count, meter.byte_in_count) == (
        8, 1, 3, 150)
    assert meter.band_stats == [BucketStats(1, 50)]
    assert parse_meter_config_reply(meter_config[0]) == [
        MeterConfig(OFPMF_KBPS | OFPMF_BURST | OFPMF_STATS, 8, 1, 1)]
    [flow] = parse_flow_stats_reply(flows[0])
    assert (flow.group_id, flow.meter_id, flow.packet_count, flow.byte_count) == (7, 8, 3, 150)

    [group_error] = blocked_group_delete
    [meter_error] = blocked_meter_delete
    assert parse_error(group_error)[:2] == (OFPET_GROUP_MOD_FAILED, OFPGMFC_GROUP_IN_USE)
    assert parse_error(meter_error)[:2] == (OFPET_METER_MOD_FAILED, OFPMMFC_METER_IN_USE)
    assert 7 in switch.groups and 8 in switch.meters


def test_unsupported_group_and_meter_variants_return_errors():
    controller = FakeController()

    async def body(sched, channel, nodes):
        group = await ask(controller, sched, 1, pack_group_mod(
            OFPGC_ADD, 1, 7, [GroupBucket(1)], xid=117))             # SELECT
        meter = await ask(controller, sched, 1, pack_meter_mod(
            OFPMC_ADD, 0, 8, rate=100, xid=118))                    # no KBPS flag
        return group, meter

    group, meter = asyncio.run(with_network(controller, body))
    assert parse_error(group[0])[:2] == (OFPET_GROUP_MOD_FAILED, OFPGMFC_BAD_TYPE)
    assert parse_error(meter[0])[:2] == (OFPET_METER_MOD_FAILED, OFPMMFC_UNSUPPORTED)


def test_desc_table_and_aggregate_stats_are_answered():
    controller = FakeController()

    async def body(sched, channel, nodes):
        await ping(sched, nodes, 1)

        async def multipart(mp_type, xid, payload=b""):
            request = Message(
                OFPT_MULTIPART_REQUEST, xid=xid,
                body=MULTIPART_HEADER.pack(mp_type, 0) + payload,
            )
            return await ask(controller, sched, 1, request)

        return (
            await multipart(OFPMP_DESC, 105),
            await multipart(OFPMP_TABLE, 106),
            await ask(controller, sched, 1, pack_aggregate_stats_request(xid=107)),
            nodes["s1"],
        )

    desc, table, aggregate, switch = asyncio.run(with_network(controller, body))
    _mp, _flags, desc_body = parse_multipart_reply(desc[0])
    assert DESC_STATS.unpack(desc_body)[-1].rstrip(b"\x00") == b"s1 (dpid=1)"
    _mp, _flags, table_body = parse_multipart_reply(table[0])
    assert TABLE_STATS_ENTRY.unpack(table_body) == (
        0, len(switch.flows), switch.table_lookups, switch.table_matched)
    stats = parse_aggregate_stats_reply(aggregate[0])
    assert stats.flow_count == len(switch.flows)


def test_unconnected_table_miss_counts_as_rx_dropped():
    sched = Scheduler()
    channel = OpenFlowChannel(sched)
    sched, channel, nodes = build_network(SPEC, scheduler=sched, channel=channel)
    nodes["h1"].ping_ip("10.0.0.3", 1)
    sched.run(until=0.1)

    assert nodes["s1"].port_stats[1]["rx_dropped"] == 1


def test_link_loss_does_not_increment_switch_port_counters():
    sched = Scheduler()
    channel = OpenFlowChannel(sched)
    sched, channel, nodes = build_network(SPEC, scheduler=sched, channel=channel)
    link = nodes["h1"].ports[1]
    link.loss = 1.0
    nodes["h1"].ping_ip("10.0.0.3", 1)
    sched.run(until=0.1)

    assert link.stats[nodes["h1"]]["dropped_loss"] > 0
    assert nodes["s1"].port_stats[1]["rx_packets"] == 0
    assert nodes["s1"].port_stats[1]["rx_dropped"] == 0


def test_link_frame_error_is_reported_in_openflow_port_stats():
    spec = copy.deepcopy(SPEC)
    spec["links"][0]["error_rate"] = 1.0
    controller = FakeController()

    async def body(sched, channel, nodes):
        nodes["h1"].send(Packet(H1, "ff:ff:ff:ff:ff:ff", "arp_req"))
        replies = await ask(controller, sched, 1, with_xid(pack_port_stats_request(1), 109))
        return [s for msg in replies for s in parse_port_stats_reply(msg)]

    [port] = asyncio.run(with_network(controller, body, spec=spec))
    assert (port.rx_packets, port.rx_dropped, port.rx_errors, port.rx_crc_err) == (0, 1, 1, 1)


def test_flow_stats_request_with_unsupported_match_field_gets_an_error():
    controller = FakeController()
    good = pack_flow_stats_request(FlowStatsRequest(OFPTT_ALL, None, {"eth_dst": H1}), xid=100)
    body_bytes = bytearray(good.body)
    body_bytes[8 + 32 + 4 + 2] = 5 << 1                                      # đổi ETH_DST thành ETH_TYPE
    bad = Message(good.type, xid=100, body=bytes(body_bytes))

    async def body(sched, channel, nodes):
        replies = await ask(controller, sched, 1, bad)
        await ping(sched, nodes, 1)
        return replies

    [error] = asyncio.run(with_network(controller, body))
    assert parse_error(error)[:2] == (OFPET_BAD_MATCH, OFPBMC_BAD_FIELD)