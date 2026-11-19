import struct

import pytest

from sdnsim.openflow.codec import (
    FLOW_MOD_FIXED, FlowSpec, pack_flow_mod, parse_flow_mod,
)
from sdnsim.openflow.header import (
    Message, OFPT_FLOW_MOD, OFPT_PACKET_IN, pack_message,
)
from sdnsim.openflow.reader import MessageReader

H1, H3 = "00:00:00:00:00:01", "00:00:00:00:00:03"

# Vị trí trong body (sau header 8 byte), theo ofp_flow_mod 1.3
TABLE_ID, COMMAND = 16, 17
IDLE, HARD, PRIORITY = 18, 20, 22
BUFFER_ID = 24
MATCH_AT = 40               # ofp_match bắt đầu ngay sau 40 byte cố định


def good():
    return pack_flow_mod({"eth_dst": H3}, 3, priority=1, idle_timeout=5.0, xid=8)


def mutated(edit):
    body = bytearray(good().body)
    edit(body)
    return Message(OFPT_FLOW_MOD, body=bytes(body))


def test_fixed_part_is_40_bytes():
    assert FLOW_MOD_FIXED.size == 40


def test_flow_mod_golden_bytes():
    msg = good()
    assert (msg.type, msg.xid) == (OFPT_FLOW_MOD, 8)
    # cookie, cookie_mask, table 0, ADD, idle=5, hard=0, priority=1,
    # buffer_id=NO_BUFFER, out_port=ANY, out_group=ANY, flags=0, đệm 2
    assert msg.body[:40] == struct.pack(
        "!QQBBHHHIIIH2x", 0, 0, 0, 0, 5, 0, 1, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0)
    # match: type=1, length=14, OXM ETH_DST (0x80000606) = 00:..:03, đệm tới 16 byte
    assert msg.body[40:56] == bytes.fromhex(
        "0001000e" "80000606" "000000000003" "0000")
    # instruction APPLY_ACTIONS (type 4, length 24) + action OUTPUT cổng 3
    assert msg.body[56:] == bytes.fromhex(
        "0004" "0018" "00000000"
        "0000" "0010" "00000003" "ffe5" "000000000000")
    assert len(msg.body) == 80


def test_in_port_match_golden_bytes():
    msg = pack_flow_mod({"eth_dst": H3, "in_port": 1}, 3)
    # match dài 22 byte (đệm tới 24): IN_PORT (field 0, 4 byte) đứng trước ETH_DST (field 3)
    assert msg.body[40:64] == bytes.fromhex(
        "00010016" "80000004" "00000001" "80000606" "000000000003" "0000")


def test_table_miss_flow_golden_bytes():
    msg = pack_flow_mod({}, "CONTROLLER", priority=0)
    assert msg.body[40:48] == bytes.fromhex("00010004" "00000000")     # match rỗng
    assert msg.body[48:] == bytes.fromhex(
        "0004" "0018" "00000000"
        "0000" "0010" "fffffffd" "ffff" "000000000000")                # max_len = NO_BUFFER
    assert parse_flow_mod(msg) == ({}, "CONTROLLER", 0, None)


@pytest.mark.parametrize("match, out_port, priority, idle", [
    ({"eth_dst": H3}, 3, 1, None),
    ({"eth_dst": H1, "eth_src": H3}, "FLOOD", 10, 30.0),
    ({"eth_src": H1}, 7, 65535, 1.0),
    ({}, "CONTROLLER", 0, None),
    ({"in_port": 1, "eth_dst": H3, "eth_src": H1}, 3, 1, None),
    ({"in_port": 2}, "FLOOD", 5, 10.0),
])
def test_flow_mod_roundtrip(match, out_port, priority, idle):
    spec = parse_flow_mod(pack_flow_mod(match, out_port, priority, idle))
    assert spec == FlowSpec(match, out_port, priority, idle)
    assert isinstance(spec.idle_timeout, (float, type(None)))


def test_flow_mod_survives_wire_framing():
    wire = pack_message(good())
    [msg] = MessageReader().feed(wire)
    assert parse_flow_mod(msg) == ({"eth_dst": H3}, 3, 1, 5.0)


def test_match_fields_are_written_in_fixed_order():
    a = pack_flow_mod({"eth_src": H1, "eth_dst": H3}, 2)
    b = pack_flow_mod({"eth_dst": H3, "eth_src": H1}, 2)
    assert a.body == b.body
    assert a.body[44:48] == bytes.fromhex("80000606")      # eth_dst (field 3) đứng trước


def test_pack_rejects_bad_inputs():
    with pytest.raises(ValueError):
        pack_flow_mod({"kind": "icmp_req"}, 1)             # trường match chưa hỗ trợ
    with pytest.raises(ValueError):
        pack_flow_mod({"eth_dst": "00:11"}, 1)             # MAC sai độ dài
    for bad_in_port in (0, -1, "1", None, True, 0xFFFFFFFD):
        with pytest.raises(ValueError):
            pack_flow_mod({"in_port": bad_in_port}, 1)
    for bad_port in (0, -1, "ALL", None, 0xFFFFFFFE):
        with pytest.raises(ValueError):
            pack_flow_mod({}, bad_port)
    with pytest.raises(ValueError):
        pack_flow_mod({}, 1, priority=70000)
    for bad_idle in (0, 0.5, 70000, -1):
        with pytest.raises(ValueError):
            pack_flow_mod({}, 1, idle_timeout=bad_idle)


def test_parse_rejects_unsupported_flow_mods():
    with pytest.raises(ValueError):                         # DELETE
        parse_flow_mod(mutated(lambda b: b.__setitem__(COMMAND, 3)))
    with pytest.raises(ValueError):                         # table 1
        parse_flow_mod(mutated(lambda b: b.__setitem__(TABLE_ID, 1)))
    with pytest.raises(ValueError):                         # hard_timeout
        parse_flow_mod(mutated(lambda b: struct.pack_into("!H", b, HARD, 9)))
    with pytest.raises(ValueError):                         # buffer_id thật
        parse_flow_mod(mutated(lambda b: struct.pack_into("!I", b, BUFFER_ID, 77)))


def test_parse_rejects_unknown_or_masked_match_field():
    # đổi ETH_DST (field 3) thành ETH_TYPE (field 5): bỏ qua sẽ khiến flow khớp rộng hơn
    with pytest.raises(ValueError):
        parse_flow_mod(mutated(lambda b: b.__setitem__(MATCH_AT + 6, 5 << 1)))
    # bật cờ mask
    with pytest.raises(ValueError):
        parse_flow_mod(mutated(lambda b: b.__setitem__(MATCH_AT + 6, (3 << 1) | 1)))


def test_parse_rejects_bad_in_port_match():
    base = pack_flow_mod({"in_port": 1}, 3)
    in_port_value_at = MATCH_AT + 8                        # sau header match (4) + header OXM (4)

    zero = bytearray(base.body)
    struct.pack_into("!I", zero, in_port_value_at, 0)      # cổng 0 không tồn tại
    with pytest.raises(ValueError):
        parse_flow_mod(Message(OFPT_FLOW_MOD, body=bytes(zero)))

    special = bytearray(base.body)
    struct.pack_into("!I", special, in_port_value_at, 0xFFFFFFFD)   # OFPP_CONTROLLER
    with pytest.raises(ValueError):
        parse_flow_mod(Message(OFPT_FLOW_MOD, body=bytes(special)))

    masked = bytearray(base.body)
    masked[MATCH_AT + 6] |= 1                              # bật cờ mask
    with pytest.raises(ValueError):
        parse_flow_mod(Message(OFPT_FLOW_MOD, body=bytes(masked)))


def test_parse_rejects_bad_instructions():
    instr_at = 56
    with pytest.raises(ValueError):                         # GOTO_TABLE thay APPLY_ACTIONS
        parse_flow_mod(mutated(lambda b: struct.pack_into("!H", b, instr_at, 1)))
    with pytest.raises(ValueError):                         # action không phải OUTPUT
        parse_flow_mod(mutated(lambda b: struct.pack_into("!H", b, instr_at + 8, 25)))
    with pytest.raises(ValueError):                         # không có instruction (drop)
        parse_flow_mod(Message(OFPT_FLOW_MOD, body=good().body[:instr_at]))

    body = good().body
    two = body[:instr_at] + struct.pack("!HH4x", 4, 8 + 32) + body[instr_at + 8:] * 2
    with pytest.raises(ValueError):                         # hai action OUTPUT
        parse_flow_mod(Message(OFPT_FLOW_MOD, body=two))


def test_parse_rejects_wrong_type_and_truncation():
    with pytest.raises(ValueError):
        parse_flow_mod(Message(OFPT_PACKET_IN, body=good().body))
    for cut in (10, 39, 41, 50, 60):
        with pytest.raises(ValueError):
            parse_flow_mod(Message(OFPT_FLOW_MOD, body=good().body[:cut]))