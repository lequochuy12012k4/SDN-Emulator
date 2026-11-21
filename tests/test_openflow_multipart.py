import pytest

from sdnsim.openflow.codec import (
    MULTIPART_HEADER, OFPMP_PORT_DESC, OFPPF_1GB_FD, OFPT_MULTIPART_REPLY,
    OFPT_MULTIPART_REQUEST, PORT_DESC_ENTRY, PortDesc,
    pack_port_desc_reply, parse_multipart_request,
)
from sdnsim.openflow.header import Message, OFPT_PACKET_IN


def test_entry_sizes_match_the_spec():
    assert MULTIPART_HEADER.size == 8
    assert PORT_DESC_ENTRY.size == 64


def test_port_desc_reply_golden_bytes():
    msg = pack_port_desc_reply([
        PortDesc(1, "02:00:00:00:01:01", "s1-eth1", 1_000_000_000),
    ], xid=5)
    assert (msg.type, msg.xid) == (OFPT_MULTIPART_REPLY, 5)
    assert msg.body[:8] == bytes.fromhex("000d" "0000" "00000000")      # PORT_DESC, không còn phần nào
    entry = msg.body[8:]
    assert len(entry) == 64
    assert entry[:4] == bytes.fromhex("00000001")                         # port_no
    assert entry[8:14] == bytes.fromhex("020000000101")                   # hw_addr
    assert entry[16:32] == b"s1-eth1".ljust(16, b"\x00")                  # name
    assert entry[32:40] == bytes(8)                                       # config, state
    assert entry[40:44] == OFPPF_1GB_FD.to_bytes(4, "big")                # curr
    assert entry[56:64] == (1_000_000).to_bytes(4, "big") * 2             # curr_speed, max_speed


def test_unlimited_port_speed_is_reported_as_unknown():
    msg = pack_port_desc_reply([PortDesc(1, "02:00:00:00:01:01", "s1-eth1")])
    entry = PORT_DESC_ENTRY.unpack_from(msg.body, MULTIPART_HEADER.size)
    assert entry[5:8] == (0, 0, 0)                                      # curr, advertised, supported
    assert entry[9:11] == (0, 0)                                        # curr_speed, max_speed


def test_empty_port_list_still_gives_a_valid_reply():
    msg = pack_port_desc_reply([], xid=1)
    assert msg.body == MULTIPART_HEADER.pack(OFPMP_PORT_DESC, 0)


def test_several_ports_are_concatenated_in_order():
    ports = [PortDesc(n, f"02:00:00:00:01:{n:02x}", f"s1-eth{n}") for n in (1, 2, 3)]
    msg = pack_port_desc_reply(ports)
    assert len(msg.body) == 8 + 3 * 64
    assert [PORT_DESC_ENTRY.unpack_from(msg.body, 8 + i * 64)[0] for i in range(3)] == [1, 2, 3]


def test_pack_rejects_bad_ports():
    for bad in (PortDesc(0, "02:00:00:00:01:01", "x"),
                PortDesc(0xFFFFFFFD, "02:00:00:00:01:01", "x"),
                PortDesc(1, "02:00:00:00:01:01", "a" * 16),
                PortDesc(1, "02:00", "x")):
        with pytest.raises(ValueError):
            pack_port_desc_reply([bad])


def test_parse_multipart_request():
    request = Message(OFPT_MULTIPART_REQUEST, xid=9, body=MULTIPART_HEADER.pack(OFPMP_PORT_DESC, 0))
    assert parse_multipart_request(request) == (OFPMP_PORT_DESC, 0, b"")
    with_body = Message(OFPT_MULTIPART_REQUEST, body=MULTIPART_HEADER.pack(4, 1) + b"abc")
    assert parse_multipart_request(with_body) == (4, 1, b"abc")


def test_parse_multipart_request_rejects_wrong_type_or_truncation():
    with pytest.raises(ValueError):
        parse_multipart_request(Message(OFPT_PACKET_IN, body=bytes(8)))
    with pytest.raises(ValueError):
        parse_multipart_request(Message(OFPT_MULTIPART_REQUEST, body=b"\x00\x0d"))