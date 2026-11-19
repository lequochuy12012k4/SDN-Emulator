import struct

import pytest

from sdnsim.core.packet import Packet
from sdnsim.openflow.codec import (
    OFP_NO_BUFFER, OFPP_ALL, OFPP_FLOOD,
    pack_packet_in, pack_packet_out, parse_packet_in, parse_packet_out,
)
from sdnsim.openflow.frame import packet_to_bytes
from sdnsim.openflow.header import (
    Message, OFPT_PACKET_IN, OFPT_PACKET_OUT, pack_message,
)
from sdnsim.openflow.reader import MessageReader

H1, H3 = "00:00:00:00:00:01", "00:00:00:00:00:03"
BCAST = "ff:ff:ff:ff:ff:ff"


def arp_req():
    return Packet(H1, BCAST, "arp_req", ip_src="10.0.0.1", ip_dst="10.0.0.3")


def icmp_req():
    return Packet(H1, H3, "icmp_req", seq=5, sent_at=0.123,
                  ip_src="10.0.0.1", ip_dst="10.0.0.3")


# ---------------------------------------------------------------- PACKET_IN

def test_packet_in_golden_bytes():
    msg = pack_packet_in(2, arp_req(), xid=9)
    data = packet_to_bytes(arp_req())
    assert (msg.type, msg.xid) == (OFPT_PACKET_IN, 9)
    # buffer_id, total_len, reason, table_id, cookie
    assert msg.body[:16] == struct.pack("!IHBBQ", OFP_NO_BUFFER, len(data), 0, 0, 0)
    # match: type=1 (OXM), length=12, OXM IN_PORT (0x80000004) = 2, đệm 4 byte
    assert msg.body[16:32] == bytes.fromhex("0001000c" "80000004" "00000002" "00000000")
    assert msg.body[32:34] == b"\x00\x00"
    assert msg.body[34:] == data
    assert len(msg.body) == 34 + len(data)


@pytest.mark.parametrize("make", [arp_req, icmp_req])
def test_packet_in_roundtrip(make):
    in_port, pkt = parse_packet_in(pack_packet_in(7, make()))
    assert in_port == 7
    assert pkt == make()


def test_packet_in_survives_wire_framing():
    wire = pack_message(pack_packet_in(3, icmp_req(), xid=4))
    [msg] = MessageReader().feed(wire)
    assert parse_packet_in(msg) == (3, icmp_req())


def test_packet_in_skips_other_oxm_fields():
    # match có thêm trường ETH_TYPE (field 5, 2 byte) đứng trước IN_PORT
    eth_type = struct.pack("!HBBH", 0x8000, 5 << 1, 2, 0x0806)
    in_port = struct.pack("!HBBI", 0x8000, 0 << 1, 4, 6)
    oxm = eth_type + in_port
    match_len = 4 + len(oxm)                                   # 18
    match = struct.pack("!HH", 1, match_len) + oxm
    match += b"\x00" * (-match_len % 8)                        # đệm tới 24
    data = packet_to_bytes(arp_req())
    body = struct.pack("!IHBBQ", OFP_NO_BUFFER, len(data), 0, 0, 0)
    body += match + b"\x00\x00" + data
    assert parse_packet_in(Message(OFPT_PACKET_IN, body=body)) == (6, arp_req())


def test_packet_in_without_in_port_is_rejected():
    eth_type = struct.pack("!HBBH", 0x8000, 5 << 1, 2, 0x0806)
    match_len = 4 + len(eth_type)
    match = struct.pack("!HH", 1, match_len) + eth_type
    match += b"\x00" * (-match_len % 8)
    data = packet_to_bytes(arp_req())
    body = struct.pack("!IHBBQ", OFP_NO_BUFFER, len(data), 0, 0, 0)
    body += match + b"\x00\x00" + data
    with pytest.raises(ValueError):
        parse_packet_in(Message(OFPT_PACKET_IN, body=body))


def test_packet_in_wrong_type_or_truncated_is_rejected():
    with pytest.raises(ValueError):
        parse_packet_in(Message(OFPT_PACKET_OUT, body=b""))
    good = pack_packet_in(1, arp_req())
    with pytest.raises(ValueError):
        parse_packet_in(Message(OFPT_PACKET_IN, body=good.body[:20]))
    with pytest.raises(ValueError):
        parse_packet_in(Message(OFPT_PACKET_IN, body=good.body[:33]))


# --------------------------------------------------------------- PACKET_OUT

def test_packet_out_golden_bytes():
    msg = pack_packet_out(3, icmp_req(), in_port=1, xid=5)
    assert (msg.type, msg.xid) == (OFPT_PACKET_OUT, 5)
    # buffer_id, in_port=1, actions_len=16, đệm 6
    assert msg.body[:16] == bytes.fromhex(
        "ffffffff" "00000001" "0010" "000000000000")
    # action OUTPUT: type=0, len=16, port=3, max_len=0xffe5, đệm 6
    assert msg.body[16:32] == bytes.fromhex(
        "0000" "0010" "00000003" "ffe5" "000000000000")
    assert msg.body[32:] == packet_to_bytes(icmp_req())


@pytest.mark.parametrize("out_port, in_port", [(3, 1), ("FLOOD", 2), (2, None)])
def test_packet_out_roundtrip(out_port, in_port):
    msg = pack_packet_out(out_port, icmp_req(), in_port=in_port)
    assert parse_packet_out(msg) == (out_port, in_port, icmp_req())


def test_flood_is_encoded_as_ofpp_flood_and_all_reads_as_flood():
    msg = pack_packet_out("FLOOD", arp_req(), in_port=1)
    assert struct.unpack_from("!I", msg.body, 20)[0] == OFPP_FLOOD
    body = bytearray(msg.body)
    struct.pack_into("!I", body, 20, OFPP_ALL)
    out, _, _ = parse_packet_out(Message(OFPT_PACKET_OUT, body=bytes(body)))
    assert out == "FLOOD"


def test_packet_out_bad_inputs_are_rejected():
    with pytest.raises(ValueError):
        pack_packet_out(0, icmp_req())
    with pytest.raises(ValueError):
        pack_packet_out("ALL", icmp_req())

    good = pack_packet_out(3, icmp_req(), in_port=1)

    buffered = bytearray(good.body)
    struct.pack_into("!I", buffered, 0, 77)            # buffer_id thật
    with pytest.raises(ValueError):
        parse_packet_out(Message(OFPT_PACKET_OUT, body=bytes(buffered)))

    other_action = bytearray(good.body)
    struct.pack_into("!H", other_action, 16, 25)       # type 25 = SET_FIELD
    with pytest.raises(ValueError):
        parse_packet_out(Message(OFPT_PACKET_OUT, body=bytes(other_action)))

    two_outputs = bytearray(good.body)
    two_outputs[8:10] = struct.pack("!H", 32)          # actions_len = 32
    two_outputs[32:32] = good.body[16:32]              # chèn thêm một action OUTPUT
    with pytest.raises(ValueError):
        parse_packet_out(Message(OFPT_PACKET_OUT, body=bytes(two_outputs)))

    with pytest.raises(ValueError):
        parse_packet_out(Message(OFPT_PACKET_IN, body=good.body))
    with pytest.raises(ValueError):
        parse_packet_out(Message(OFPT_PACKET_OUT, body=good.body[:10]))