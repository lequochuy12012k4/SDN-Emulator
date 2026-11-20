import pytest

from sdnsim.openflow.codec import (
    ERROR_DATA_BYTES, OFPBAC_BAD_OUT_PORT, OFPET_BAD_ACTION, OFPT_ERROR,
    pack_error, parse_error,
)
from sdnsim.openflow.header import Message, OFPT_FLOW_MOD, OFPT_PACKET_IN, pack_message


def test_error_copies_xid_and_carries_start_of_request():
    request = Message(OFPT_FLOW_MOD, xid=77, body=b"\x11" * 20)
    msg = pack_error(OFPET_BAD_ACTION, OFPBAC_BAD_OUT_PORT, request)
    assert (msg.type, msg.xid) == (OFPT_ERROR, 77)
    assert msg.body[:4] == bytes.fromhex("0002" "0004")              # type=2, code=4
    assert msg.body[4:] == pack_message(request)                      # ngắn hơn 64 byte: gửi đủ


def test_error_data_is_cut_to_64_bytes():
    request = Message(OFPT_FLOW_MOD, xid=1, body=b"\x22" * 200)
    msg = pack_error(5, 0, request)
    assert len(msg.body) == 4 + ERROR_DATA_BYTES
    assert msg.body[4:] == pack_message(request)[:ERROR_DATA_BYTES]


def test_error_roundtrip():
    request = Message(OFPT_FLOW_MOD, xid=9, body=b"abc")
    err_type, code, data = parse_error(pack_error(1, 1, request))
    assert (err_type, code, data) == (1, 1, pack_message(request))


def test_parse_error_rejects_wrong_type_or_truncation():
    with pytest.raises(ValueError):
        parse_error(Message(OFPT_PACKET_IN, body=b"\x00\x01\x00\x01"))
    with pytest.raises(ValueError):
        parse_error(Message(OFPT_ERROR, body=b"\x00"))