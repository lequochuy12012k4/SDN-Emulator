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
    OFPMP_PORT_DESC, OFPT_BARRIER_REPLY, PortDesc, pack_error, pack_flow_mod,
    pack_packet_in, pack_packet_out, pack_port_desc_reply, parse_flow_mod,
    parse_multipart_request, parse_packet_out,
)
from sdnsim.openflow.frame import packet_to_bytes                       # noqa: E402
from sdnsim.openflow.header import (                                    # noqa: E402
    Message, OFPT_FEATURES_REPLY, OFPT_FLOW_MOD, pack_message,
)
from sdnsim.openflow.reader import MessageReader                        # noqa: E402
from sdnsim.openflow.session import FEATURES_REPLY_BODY                 # noqa: E402

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
    body = FEATURES_REPLY_BODY.pack(0xABC, 0, 1, 0, 0, 0)
    m = os_ken_reads(Message(OFPT_FEATURES_REPLY, xid=3, body=body))
    assert type(m).__name__ == "OFPSwitchFeatures"
    assert (m.xid, m.datapath_id, m.n_tables) == (3, 0xABC, 1)


def test_os_ken_reads_our_error_messages():
    request = Message(OFPT_FLOW_MOD, xid=77, body=b"\x00" * 100)
    m = os_ken_reads(pack_error(OFPET_FLOW_MOD_FAILED, OFPFMFC_UNKNOWN, request))
    assert type(m).__name__ == "OFPErrorMsg"
    assert (m.xid, m.type, m.code) == (77, ofp.OFPET_FLOW_MOD_FAILED, ofp.OFPFMFC_UNKNOWN)
    assert bytes(m.data) == pack_message(request)[:64]

    m = os_ken_reads(pack_error(OFPET_BAD_REQUEST, OFPBRC_BAD_TYPE, request))
    assert (m.type, m.code) == (ofp.OFPET_BAD_REQUEST, ofp.OFPBRC_BAD_TYPE)


def test_os_ken_reads_our_port_desc_reply():
    ports = [PortDesc(1, "02:00:00:00:01:01", "s1-eth1"), PortDesc(3, "02:00:00:00:01:03", "s1-eth3")]
    m = os_ken_reads(pack_port_desc_reply(ports, xid=83))
    assert type(m).__name__ == "OFPPortDescStatsReply"
    assert (m.xid, m.type, m.flags) == (83, ofp.OFPMP_PORT_DESC, 0)
    assert [p.port_no for p in m.body] == [1, 3]
    assert [p.hw_addr for p in m.body] == ["02:00:00:00:01:01", "02:00:00:00:01:03"]
    assert [p.name.rstrip(b"\x00") for p in m.body] == [b"s1-eth1", b"s1-eth3"]
    assert all(p.curr == ofp.OFPPF_1GB_FD and p.curr_speed == 1_000_000 for p in m.body)


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
    assert parse_flow_mod(msg) == expected


def test_we_read_os_ken_flow_mod_with_in_port():
    """simple_switch_13 kiểu Ryu luôn match thêm in_port: ta phải đọc được."""
    msg = we_read(parser.OFPFlowMod(
        datapath=DP, match=parser.OFPMatch(in_port=1, eth_dst=H3, eth_src=H1),
        instructions=apply([parser.OFPActionOutput(3)]), priority=1))
    assert parse_flow_mod(msg) == ({"in_port": 1, "eth_dst": H3, "eth_src": H1}, 3, 1, None)


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