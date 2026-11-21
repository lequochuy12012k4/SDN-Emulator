"""Đối chiếu bộ mã hóa/giải mã của ta với os-ken, một bản cài đặt OpenFlow độc lập.

Mục đích: các test golden khác chỉ khóa những gì CHÚNG TA hiểu về đặc tả. Test này
kiểm tra hiểu biết đó với một thư viện bên ngoài. Bỏ qua tự động nếu chưa cài os-ken.

os-ken chỉ có bộ đọc cho thông điệp switch -> controller (PACKET_IN, FEATURES_REPLY),
còn PACKET_OUT và FLOW_MOD là thông điệp controller -> switch nên nó chỉ có bộ ghi.
Vì vậy: ta để os-ken ĐỌC thông điệp ta gửi, và để ta ĐỌC thông điệp os-ken ghi.
"""

import struct

import pytest

pytest.importorskip("os_ken")

from os_ken.ofproto import ofproto_parser, ofproto_protocol            # noqa: E402
from os_ken.ofproto import ofproto_v1_3 as ofp                          # noqa: E402
from os_ken.ofproto import ofproto_v1_3_parser as parser                # noqa: E402

from sdnsim.core.packet import Packet                                   # noqa: E402
from sdnsim.openflow.codec import (                                     # noqa: E402
    OFPBRC_BAD_TYPE, OFPET_BAD_REQUEST, OFPET_FLOW_MOD_FAILED, OFPFMFC_UNKNOWN,
    OFPMP_AGGREGATE, OFPMP_DESC, OFPMP_FLOW, OFPMP_GROUP, OFPMP_METER, OFPMP_PORT_DESC,
    OFPMP_PORT_STATS, OFPMP_TABLE, OFPMP_GROUP_DESC, OFPMP_METER_CONFIG,
    OFPT_BARRIER_REPLY, AggregateStats, FlowStats, BucketStats, FlowSpec, FlowStatsRequest,
    GroupBucket, GroupDesc, GroupModSpec, GroupStats,
    MeterConfig, MeterModSpec, MeterStats, PortDesc, PortStats, TableStats,
    OFPPF_OTHER,
    OFPGC_ADD, OFPGT_ALL, OFPMF_BURST, OFPMF_KBPS, OFPMF_STATS, OFPMC_ADD,
    pack_group_mod, pack_meter_mod, pack_aggregate_stats_reply,
    pack_aggregate_stats_request, pack_desc_stats_reply, pack_error, pack_flow_mod, pack_flow_stats_reply,
    pack_packet_in, pack_packet_out, pack_port_desc_reply, pack_port_stats_reply,
    pack_group_stats_reply, pack_meter_stats_reply, pack_table_stats_reply,
    pack_group_desc_reply, pack_meter_config_reply, pack_group_desc_request,
    pack_meter_config_request, parse_group_mod, parse_meter_mod,
    parse_flow_mod, parse_flow_mod_cookie, parse_flow_stats_request,
    parse_group_desc_request, parse_group_stats_request, parse_meter_stats_request,
    parse_multipart_request,
    parse_packet_out, parse_port_stats_request,
)
from sdnsim.openflow.frame import packet_to_bytes                       # noqa: E402
from sdnsim.openflow.header import (                                    # noqa: E402
    Message, OFPT_FEATURES_REQUEST, OFPT_FLOW_MOD, pack_message,
)
from sdnsim.openflow.reader import MessageReader                        # noqa: E402
from sdnsim.openflow.session import (                                    # noqa: E402
    OpenFlowSession, SUPPORTED_CAPABILITIES,
)

H1, H3 = "00:00:00:00:00:01", "00:00:00:00:00:03"
DP = ofproto_protocol.ProtocolDesc(version=ofp.OFP_VERSION)


def pkt():
    return Packet(H1, H3, "icmp_req", seq=5, sent_at=0.1,
                  ip_src="10.0.0.1", ip_dst="10.0.0.3")


def os_ken_reads(msg: Message):
    """Cho os-ken đọc một thông điệp của ta."""
    buf = pack_message(msg)
    version, msg_type, msg_len, xid = struct.unpack_from("!BBHI", buf)
    return ofproto_parser.msg(DP, version, msg_type, msg_len, xid, buf)


def we_read(os_ken_msg) -> Message:
    """Cho os-ken ghi một thông điệp, rồi lấy qua bộ đọc khung của ta."""
    os_ken_msg.serialize()
    [msg] = MessageReader().feed(bytes(os_ken_msg.buf))
    return msg


def apply(actions):
    return [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]


# ---------------------------------------- ta ghi, os-ken đọc (switch -> controller)

def test_os_ken_reads_our_packet_in():
    m = os_ken_reads(pack_packet_in(2, pkt(), xid=9))
    assert type(m).__name__ == "OFPPacketIn"
    assert m.xid == 9
    assert m.match["in_port"] == 2
    assert m.reason == ofp.OFPR_NO_MATCH
    assert m.buffer_id == ofp.OFP_NO_BUFFER
    assert bytes(m.data) == packet_to_bytes(pkt())


def test_os_ken_reads_our_features_reply():
    class Writer:
        def write(self, data):
            self.data = data

    session = OpenFlowSession(0xABC)
    session._writer = Writer()
    assert session._handle(Message(OFPT_FEATURES_REQUEST, xid=3))
    [reply] = MessageReader().feed(session._writer.data)
    m = os_ken_reads(reply)
    assert type(m).__name__ == "OFPSwitchFeatures"
    assert (m.xid, m.datapath_id, m.n_tables) == (3, 0xABC, 1)
    assert m.capabilities == SUPPORTED_CAPABILITIES


def test_os_ken_reads_our_error_messages():
    request = Message(OFPT_FLOW_MOD, xid=77, body=b"\x00" * 100)
    m = os_ken_reads(pack_error(OFPET_FLOW_MOD_FAILED, OFPFMFC_UNKNOWN, request))
    assert type(m).__name__ == "OFPErrorMsg"
    assert (m.xid, m.type, m.code) == (77, ofp.OFPET_FLOW_MOD_FAILED, ofp.OFPFMFC_UNKNOWN)
    assert bytes(m.data) == pack_message(request)[:64]

    m = os_ken_reads(pack_error(OFPET_BAD_REQUEST, OFPBRC_BAD_TYPE, request))
    assert (m.type, m.code) == (ofp.OFPET_BAD_REQUEST, ofp.OFPBRC_BAD_TYPE)


def test_os_ken_reads_our_port_desc_reply():
    ports = [
        PortDesc(1, "02:00:00:00:01:01", "s1-eth1", 1_000_000_000),
        PortDesc(2, "02:00:00:00:01:02", "s1-eth2", 100_000_000),
        PortDesc(3, "02:00:00:00:01:03", "s1-eth3"),
    ]
    m = os_ken_reads(pack_port_desc_reply(ports, xid=83))
    assert type(m).__name__ == "OFPPortDescStatsReply"
    assert (m.xid, m.type, m.flags) == (83, ofp.OFPMP_PORT_DESC, 0)
    assert [p.port_no for p in m.body] == [1, 2, 3]
    assert [p.hw_addr for p in m.body] == [
        "02:00:00:00:01:01", "02:00:00:00:01:02", "02:00:00:00:01:03"]
    assert [p.name.rstrip(b"\x00") for p in m.body] == [
        b"s1-eth1", b"s1-eth2", b"s1-eth3"]
    assert [p.curr for p in m.body] == [
        ofp.OFPPF_1GB_FD, ofp.OFPPF_100MB_FD, 0]
    assert [p.supported for p in m.body] == [p.curr for p in m.body]
    assert [p.curr_speed for p in m.body] == [1_000_000, 100_000, 0]
    assert [p.max_speed for p in m.body] == [1_000_000, 100_000, 0]


def test_os_ken_reads_custom_port_speed_as_other():
    msg = pack_port_desc_reply([
        PortDesc(1, "02:00:00:00:01:01", "s1-eth1", 100_000),
    ], xid=84)
    [port] = os_ken_reads(msg).body
    assert (port.curr, port.supported, port.curr_speed, port.max_speed) == (
        OFPPF_OTHER, OFPPF_OTHER, 100, 100)


def test_os_ken_reads_our_port_stats_reply():
    stats = [PortStats(1, 10, 20, 500, 1000, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10), PortStats(3, tx_dropped=7)]
    [msg] = pack_port_stats_reply(stats, xid=90)
    m = os_ken_reads(msg)
    assert type(m).__name__ == "OFPPortStatsReply"
    assert (m.xid, m.type, m.flags) == (90, ofp.OFPMP_PORT_STATS, 0)
    assert [tuple(getattr(p, f) for f in (
        "port_no", "rx_packets", "tx_packets", "rx_bytes", "tx_bytes", "rx_dropped", "tx_dropped",
        "rx_errors", "tx_errors", "rx_frame_err", "rx_over_err", "rx_crc_err", "collisions",
        "duration_sec", "duration_nsec")) for p in m.body] == [tuple(s) for s in stats]


def test_os_ken_reads_our_flow_stats_reply():
    flows = [FlowStats({"in_port": 1, "eth_dst": H3, "eth_src": H1}, 3, 1, 5.0, 4, 200, 2, 500, 0xA1),
             FlowStats({}, "CONTROLLER", 0, None, 0, 0),
             FlowStats({"eth_dst": H3}, None, 10, None, 3, 150,
                       group_id=7, meter_id=8)]
    [msg] = pack_flow_stats_reply(flows, xid=91)
    m = os_ken_reads(msg)
    assert type(m).__name__ == "OFPFlowStatsReply"
    first, second, third = m.body
    assert (first.priority, first.idle_timeout, first.hard_timeout, first.table_id) == (1, 5, 0, 0)
    assert (first.packet_count, first.byte_count, first.duration_sec, first.duration_nsec) == (4, 200, 2, 500)
    assert first.cookie == 0xA1
    assert dict(first.match.items()) == {"in_port": 1, "eth_dst": H3, "eth_src": H1}
    [instruction] = first.instructions
    assert [(a.type, a.port) for a in instruction.actions] == [(ofp.OFPAT_OUTPUT, 3)]
    assert dict(second.match.items()) == {}
    assert second.instructions[0].actions[0].port == ofp.OFPP_CONTROLLER
    assert (third.instructions[0].meter_id, third.instructions[1].actions[0].group_id) == (8, 7)


def test_we_read_os_ken_stats_requests():
    request = we_read(parser.OFPPortStatsRequest(datapath=DP, flags=0, port_no=ofp.OFPP_ANY))
    mp_type, _flags, body = parse_multipart_request(request)
    assert (mp_type, parse_port_stats_request(body)) == (ofp.OFPMP_PORT_STATS, ofp.OFPP_ANY)

    request = we_read(parser.OFPFlowStatsRequest(
        datapath=DP, flags=0, table_id=ofp.OFPTT_ALL, out_port=3, out_group=ofp.OFPG_ANY,
        cookie=0xA0, cookie_mask=0xF0, match=parser.OFPMatch(eth_dst=H3)))
    mp_type, _flags, body = parse_multipart_request(request)
    assert mp_type == ofp.OFPMP_FLOW
    assert parse_flow_stats_request(body) == FlowStatsRequest(
        ofp.OFPTT_ALL, 3, {"eth_dst": H3}, 0xA0, 0xF0)

    request = we_read(parser.OFPFlowStatsRequest(datapath=DP))          # mặc định của os-ken: mọi flow
    _mp, _flags, body = parse_multipart_request(request)
    assert parse_flow_stats_request(body).match == {} and parse_flow_stats_request(body).table_id == ofp.OFPTT_ALL


def test_os_ken_reads_our_added_multipart_replies():
    [desc] = pack_desc_stats_reply("s1", 1, xid=110)
    msg = os_ken_reads(desc)
    assert type(msg).__name__ == "OFPDescStatsReply"
    assert msg.xid == 110 and msg.body.dp_desc.rstrip(b"\x00") == b"s1 (dpid=1)"

    [table] = pack_table_stats_reply([TableStats(0, 2, 10, 8)], xid=111)
    msg = os_ken_reads(table)
    assert type(msg).__name__ == "OFPTableStatsReply"
    assert [(s.table_id, s.active_count, s.lookup_count, s.matched_count) for s in msg.body] == [
        (0, 2, 10, 8)]

    [aggregate] = pack_aggregate_stats_reply(AggregateStats(8, 400, 2), xid=112)
    msg = os_ken_reads(aggregate)
    assert type(msg).__name__ == "OFPAggregateStatsReply"
    assert (msg.body.packet_count, msg.body.byte_count, msg.body.flow_count) == (8, 400, 2)

    [group] = pack_group_stats_reply([
        GroupStats(7, 1, 2, 100, 3, 4, [BucketStats(1, 50), BucketStats(1, 50)])
    ], xid=113)
    msg = os_ken_reads(group)
    assert type(msg).__name__ == "OFPGroupStatsReply"
    [group_stats] = msg.body
    assert (group_stats.group_id, group_stats.ref_count, group_stats.packet_count,
            group_stats.byte_count) == (7, 1, 2, 100)
    assert [(b.packet_count, b.byte_count) for b in group_stats.bucket_stats] == [
        (1, 50), (1, 50)]

    [meter] = pack_meter_stats_reply([
        MeterStats(8, 1, 3, 150, 2, 3, [BucketStats(1, 50)])
    ], xid=114)
    msg = os_ken_reads(meter)
    assert type(msg).__name__ == "OFPMeterStatsReply"
    [meter_stats] = msg.body
    assert (meter_stats.meter_id, meter_stats.flow_count, meter_stats.packet_in_count,
            meter_stats.byte_in_count) == (8, 1, 3, 150)
    assert [(b.packet_band_count, b.byte_band_count) for b in meter_stats.band_stats] == [(1, 50)]


def test_os_ken_reads_our_group_desc_and_meter_config_replies():
    [group] = pack_group_desc_reply([
        GroupDesc(OFPGT_ALL, 7, [GroupBucket(1), GroupBucket(2)])
    ], xid=115)
    msg = os_ken_reads(group)
    assert type(msg).__name__ == "OFPGroupDescStatsReply"
    [group_desc] = msg.body
    assert (group_desc.type, group_desc.group_id) == (OFPGT_ALL, 7)
    assert [bucket.actions[0].port for bucket in group_desc.buckets] == [1, 2]

    [meter] = pack_meter_config_reply([MeterConfig(
        OFPMF_KBPS | OFPMF_BURST | OFPMF_STATS, 8, 100, 10)], xid=116)
    msg = os_ken_reads(meter)
    assert type(msg).__name__ == "OFPMeterConfigStatsReply"
    [config] = msg.body
    assert (config.flags, config.meter_id) == (OFPMF_KBPS | OFPMF_BURST | OFPMF_STATS, 8)
    assert (config.bands[0].rate, config.bands[0].burst_size) == (100, 10)


def test_we_read_os_ken_group_and_meter_mods():
    bucket = parser.OFPBucket(actions=[parser.OFPActionOutput(1)])
    os_group = parser.OFPGroupMod(DP, OFPGC_ADD, OFPGT_ALL, 7, [bucket])
    assert parse_group_mod(we_read(os_group)) == GroupModSpec(
        OFPGC_ADD, OFPGT_ALL, 7, [GroupBucket(1)])

    os_meter = parser.OFPMeterMod(
        DP, OFPMC_ADD, OFPMF_KBPS | OFPMF_BURST | OFPMF_STATS, 8,
        [parser.OFPMeterBandDrop(100, 10)])
    assert parse_meter_mod(we_read(os_meter)) == MeterModSpec(
        OFPMC_ADD, OFPMF_KBPS | OFPMF_BURST | OFPMF_STATS, 8, 100, 10)
    assert parse_meter_mod(pack_meter_mod(
        OFPMC_ADD, OFPMF_KBPS | OFPMF_BURST | OFPMF_STATS, 8, 100, 10)) == MeterModSpec(
            OFPMC_ADD, OFPMF_KBPS | OFPMF_BURST | OFPMF_STATS, 8, 100, 10)


def test_we_read_os_ken_flow_mod_with_group_and_meter():
    instructions = [
        parser.OFPInstructionMeter(8),
        parser.OFPInstructionActions(
            ofp.OFPIT_APPLY_ACTIONS, [parser.OFPActionGroup(7)]),
    ]
    msg = we_read(parser.OFPFlowMod(
        datapath=DP, match=parser.OFPMatch(eth_dst=H3), instructions=instructions,
        priority=10))
    assert parse_flow_mod(msg) == FlowSpec({"eth_dst": H3}, None, 10, None, 7, 8)

    ours = pack_flow_mod({"eth_dst": H3}, None, priority=10, group_id=7, meter_id=8)
    os_flow = parser.OFPFlowMod(
        datapath=DP, match=parser.OFPMatch(eth_dst=H3), instructions=instructions,
        priority=10, out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY)
    assert ours.body == we_read(os_flow).body


def test_os_ken_reads_our_group_and_meter_mods():
    group = pack_group_mod(OFPGC_ADD, OFPGT_ALL, 7, [
        GroupBucket(1), GroupBucket(2),
    ])
    os_group = parser.OFPGroupMod(
        DP, OFPGC_ADD, OFPGT_ALL, 7, [
            parser.OFPBucket(actions=[parser.OFPActionOutput(1)]),
            parser.OFPBucket(actions=[parser.OFPActionOutput(2)]),
        ])
    assert group.body == we_read(os_group).body

    meter = pack_meter_mod(
        OFPMC_ADD, OFPMF_KBPS | OFPMF_BURST | OFPMF_STATS, 8, 100, 10)
    os_meter = parser.OFPMeterMod(
        DP, OFPMC_ADD, OFPMF_KBPS | OFPMF_BURST | OFPMF_STATS, 8,
        [parser.OFPMeterBandDrop(100, 10)])
    assert meter.body == we_read(os_meter).body


def test_we_read_os_ken_added_multipart_requests():
    requests = [
        (parser.OFPDescStatsRequest(DP), OFPMP_DESC, b""),
        (parser.OFPTableStatsRequest(DP), OFPMP_TABLE, b""),
        (parser.OFPAggregateStatsRequest(
            DP, 0, ofp.OFPTT_ALL, ofp.OFPP_ANY, ofp.OFPG_ANY, 0xA0, 0xF0,
            parser.OFPMatch(eth_dst=H3)), OFPMP_AGGREGATE, None),
        (parser.OFPGroupStatsRequest(DP, group_id=ofp.OFPG_ALL), OFPMP_GROUP, None),
        (parser.OFPGroupDescStatsRequest(DP), OFPMP_GROUP_DESC, b""),
        (parser.OFPMeterStatsRequest(DP, meter_id=ofp.OFPM_ALL), OFPMP_METER, None),
        (parser.OFPMeterConfigStatsRequest(DP, meter_id=ofp.OFPM_ALL),
         OFPMP_METER_CONFIG, None),
    ]
    for osken_request, expected_type, expected_body in requests:
        msg = we_read(osken_request)
        mp_type, _flags, body = parse_multipart_request(msg)
        assert mp_type == expected_type
        if expected_body is not None:
            assert body == expected_body
        elif expected_type == OFPMP_AGGREGATE:
            assert parse_flow_stats_request(body) == FlowStatsRequest(
                ofp.OFPTT_ALL, None, {"eth_dst": H3}, 0xA0, 0xF0)
        elif expected_type == OFPMP_GROUP:
            assert parse_group_stats_request(body) == ofp.OFPG_ALL
        elif expected_type == OFPMP_GROUP_DESC:
            assert parse_group_desc_request(body) is None
        elif expected_type == OFPMP_METER:
            assert parse_meter_stats_request(body) == ofp.OFPM_ALL
        elif expected_type == OFPMP_METER_CONFIG:
            assert parse_meter_stats_request(body) == ofp.OFPM_ALL

    ours = pack_group_desc_request(xid=6)
    theirs = we_read(parser.OFPGroupDescStatsRequest(DP))
    assert ours.body == theirs.body
    ours = pack_meter_config_request(xid=6)
    theirs = we_read(parser.OFPMeterConfigStatsRequest(DP, meter_id=ofp.OFPM_ALL))
    assert ours.body == theirs.body

    ours = pack_aggregate_stats_request(
        FlowStatsRequest(ofp.OFPTT_ALL, None, {"eth_dst": H3}, 0xA0, 0xF0))
    theirs = we_read(parser.OFPAggregateStatsRequest(
        DP, 0, ofp.OFPTT_ALL, ofp.OFPP_ANY, ofp.OFPG_ANY, 0xA0, 0xF0,
        parser.OFPMatch(eth_dst=H3)))
    assert ours.body == theirs.body


def test_we_read_os_ken_port_desc_request():
    msg = we_read(parser.OFPPortDescStatsRequest(datapath=DP, flags=0))
    assert parse_multipart_request(msg) == (OFPMP_PORT_DESC, 0, b"")


def test_os_ken_reads_our_barrier_reply():
    m = os_ken_reads(Message(OFPT_BARRIER_REPLY, xid=80))
    assert type(m).__name__ == "OFPBarrierReply"
    assert m.xid == 80


# ------------------------------- os-ken ghi, ta đọc (controller -> switch)

@pytest.mark.parametrize("match, actions, priority, idle, expected", [
    (parser.OFPMatch(eth_dst=H3), [parser.OFPActionOutput(3)], 1, 5,
     ({"eth_dst": H3}, 3, 1, 5.0)),
    (parser.OFPMatch(eth_dst=H1, eth_src=H3), [parser.OFPActionOutput(2)], 10, 30,
     ({"eth_dst": H1, "eth_src": H3}, 2, 10, 30.0)),
    (parser.OFPMatch(eth_dst=H3), [parser.OFPActionOutput(ofp.OFPP_FLOOD)], 1, 0,
     ({"eth_dst": H3}, "FLOOD", 1, None)),
    # flow table-miss mà controller thường cài ngay khi kết nối
    (parser.OFPMatch(), [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)],
     0, 0, ({}, "CONTROLLER", 0, None)),
])
def test_we_read_os_ken_flow_mod(match, actions, priority, idle, expected):
    msg = we_read(parser.OFPFlowMod(datapath=DP, match=match, instructions=apply(actions),
                                    priority=priority, idle_timeout=idle))
    assert parse_flow_mod(msg) == FlowSpec(*expected)


def test_we_read_os_ken_flow_mod_with_in_port():
    """simple_switch_13 kiểu Ryu luôn match thêm in_port: ta phải đọc được."""
    msg = we_read(parser.OFPFlowMod(
        datapath=DP, match=parser.OFPMatch(in_port=1, eth_dst=H3, eth_src=H1),
        instructions=apply([parser.OFPActionOutput(3)]), priority=1))
    assert parse_flow_mod(msg) == FlowSpec(
        {"in_port": 1, "eth_dst": H3, "eth_src": H1}, 3, 1, None)


def test_we_read_os_ken_packet_out():
    data = packet_to_bytes(pkt())
    msg = we_read(parser.OFPPacketOut(
        datapath=DP, buffer_id=ofp.OFP_NO_BUFFER, in_port=1,
        actions=[parser.OFPActionOutput(3)], data=data))
    assert parse_packet_out(msg) == (3, 1, pkt())


def test_we_read_os_ken_packet_out_flood_from_controller():
    data = packet_to_bytes(pkt())
    msg = we_read(parser.OFPPacketOut(
        datapath=DP, buffer_id=ofp.OFP_NO_BUFFER, in_port=ofp.OFPP_CONTROLLER,
        actions=[parser.OFPActionOutput(ofp.OFPP_FLOOD)], data=data))
    assert parse_packet_out(msg) == ("FLOOD", None, pkt())


def test_os_ken_and_we_agree_on_our_own_flow_mod_bytes():
    ours = pack_flow_mod({"eth_dst": H3}, 3, priority=1, idle_timeout=5.0, xid=8)
    theirs = we_read(parser.OFPFlowMod(
        datapath=DP, match=parser.OFPMatch(eth_dst=H3),
        instructions=apply([parser.OFPActionOutput(3)]),
        priority=1, idle_timeout=5,
        # Với lệnh ADD, out_port/out_group bị switch bỏ qua. os-ken mặc định 0, còn ta
        # ghi ANY (giá trị thường dùng); đặt cùng giá trị để so từng byte.
        out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY))
    # Các trường còn lại (max_len 0xffe5, cookie, flags...) phải trùng từng byte.
    assert ours.body == theirs.body
    assert pack_packet_out(3, pkt(), in_port=1).body == we_read(parser.OFPPacketOut(
        datapath=DP, buffer_id=ofp.OFP_NO_BUFFER, in_port=1,
        actions=[parser.OFPActionOutput(3)], data=packet_to_bytes(pkt()))).body


def test_os_ken_and_we_agree_on_flow_mod_bytes_with_in_port():
    ours = pack_flow_mod({"in_port": 1, "eth_dst": H3, "eth_src": H1}, 3, priority=1)
    theirs = we_read(parser.OFPFlowMod(
        datapath=DP, match=parser.OFPMatch(in_port=1, eth_dst=H3, eth_src=H1),
        instructions=apply([parser.OFPActionOutput(3)]), priority=1,
        out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY))
    assert ours.body == theirs.body


def test_flow_mod_cookie_roundtrip():
    msg = pack_flow_mod({"eth_dst": H3}, 3, cookie=0xA1)
    assert parse_flow_mod_cookie(msg) == 0xA1