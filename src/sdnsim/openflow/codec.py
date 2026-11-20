"""Đóng gói / giải gói PACKET_IN, PACKET_OUT và FLOW_MOD của OpenFlow 1.3.

Phạm vi cố ý hẹp:
  - PACKET_IN: luôn gửi kèm đủ dữ liệu (buffer_id = NO_BUFFER), match chỉ có IN_PORT.
    Khi đọc, các trường OXM khác được bỏ qua, IN_PORT là bắt buộc.
  - PACKET_OUT: đúng một action OUTPUT, không dùng buffer, kèm dữ liệu gói.
    FLOOD và ALL đều đọc thành "FLOOD" (switch mô phỏng loại trừ cổng vào).
  - FLOW_MOD: chỉ command ADD, một bảng (table 0), match gồm in_port/eth_dst/eth_src,
    đúng một instruction APPLY_ACTIONS chứa đúng một action OUTPUT.
    Khi đọc, trường match lạ bị TỪ CHỐI (không bỏ qua như PACKET_IN), vì bỏ qua sẽ
    làm flow khớp rộng hơn controller định. Cờ (flags) và cookie được bỏ qua.
"""

import struct
from typing import Callable, NamedTuple

from sdnsim.core.packet import Packet

from .frame import bytes_to_packet, packet_to_bytes
from .header import OFPT_FLOW_MOD, OFPT_PACKET_IN, OFPT_PACKET_OUT, Message, pack_message

OFP_NO_BUFFER = 0xFFFFFFFF

OFPP_MAX = 0xFFFFFF00           # số cổng vật lý hợp lệ lớn nhất
OFPP_FLOOD = 0xFFFFFFFB
OFPP_ALL = 0xFFFFFFFC
OFPP_CONTROLLER = 0xFFFFFFFD
OFPP_ANY = 0xFFFFFFFF
OFPG_ANY = 0xFFFFFFFF

OFPR_NO_MATCH = 0               # lý do PACKET_IN: table-miss
OFPR_ACTION = 1

OFPMT_OXM = 1
OFPXMC_OPENFLOW_BASIC = 0x8000
OXM_OF_IN_PORT = 0
OXM_OF_ETH_DST = 3
OXM_OF_ETH_SRC = 4

OFPAT_OUTPUT = 0
OFPCML_MAX = 0xFFE5
OFPCML_NO_BUFFER = 0xFFFF

OFPFC_ADD = 0
OFPIT_APPLY_ACTIONS = 4

# Thông điệp ngoài header.py
OFPT_ERROR = 1
OFPT_SET_CONFIG = 9
OFPT_BARRIER_REQUEST = 20
OFPT_BARRIER_REPLY = 21
OFPT_MULTIPART_REQUEST = 18
OFPT_MULTIPART_REPLY = 19
OFPMP_PORT_DESC = 13            # loại multipart duy nhất ta hỗ trợ
OFPPF_1GB_FD = 1 << 5

# Loại và mã lỗi của ERROR (đã đối chiếu với os-ken)
OFPET_BAD_REQUEST = 1
OFPET_BAD_ACTION = 2
OFPET_FLOW_MOD_FAILED = 5
OFPBRC_BAD_TYPE = 1             # thông điệp không được hỗ trợ
OFPBRC_BAD_MULTIPART = 2        # loại multipart không được hỗ trợ
OFPBRC_BAD_LEN = 6              # độ dài thông điệp sai
OFPBAC_BAD_TYPE = 0             # action không được hỗ trợ
OFPBAC_BAD_OUT_PORT = 4         # cổng đích không tồn tại
OFPFMFC_UNKNOWN = 0             # FLOW_MOD bị từ chối, không rõ mã chi tiết
ERROR_DATA_BYTES = 64           # số byte đầu của thông điệp gây lỗi được gửi kèm

PACKET_IN_FIXED = struct.Struct("!IHBBQ")     # buffer_id, total_len, reason, table_id, cookie
MATCH_HEADER = struct.Struct("!HH")           # type, length (length không tính phần đệm)
OXM_HEADER = struct.Struct("!HBB")            # class, field<<1|hasmask, length của giá trị

PACKET_OUT_FIXED = struct.Struct("!IIH6x")    # buffer_id, in_port, actions_len, đệm 6
ACTION_HEADER = struct.Struct("!HH")          # type, length
ACTION_OUTPUT = struct.Struct("!IH6x")        # port, max_len, đệm 6  (cả action = 16 byte)

# cookie, cookie_mask, table_id, command, idle, hard, priority, buffer_id, out_port,
# out_group, flags, đệm 2  (40 byte, đứng ngay sau header, trước match)
FLOW_MOD_FIXED = struct.Struct("!QQBBHHHIIIH2x")
MULTIPART_HEADER = struct.Struct("!HH4x")     # type, flags, đệm 4 (request và reply)
# port_no, đệm 4, hw_addr, đệm 2, name[16], config, state, curr, advertised, supported,
# peer, curr_speed, max_speed  (64 byte)
PORT_DESC_ENTRY = struct.Struct("!I4x6s2x16sIIIIIIII")
INSTRUCTION_HEADER = struct.Struct("!HH4x")   # type, length, đệm 4 (instruction actions)


class FlowSpec(NamedTuple):
    match: dict
    out_port: int | str
    priority: int
    idle_timeout: float | None


# ------------------------------------------------------------ hàm dùng chung

def _mac_to_bytes(mac: str) -> bytes:
    raw = bytes.fromhex(mac.replace(":", ""))
    if len(raw) != 6:
        raise ValueError(f"MAC không hợp lệ: {mac!r}")
    return raw


def _bytes_to_mac(raw: bytes) -> str:
    return ":".join(f"{b:02x}" for b in raw)


def _port_to_bytes(port: int) -> bytes:
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port <= OFPP_MAX:
        raise ValueError(f"in_port không hợp lệ: {port!r}")
    return struct.pack("!I", port)


def _bytes_to_port(raw: bytes) -> int:
    port = struct.unpack("!I", raw)[0]
    if not 0 < port <= OFPP_MAX:
        raise ValueError(f"in_port không hợp lệ: {port}")
    return port


class _Field(NamedTuple):
    number: int                  # số hiệu trường OXM
    length: int                  # độ dài giá trị (byte)
    encode: Callable             # giá trị Python -> byte
    decode: Callable             # byte -> giá trị Python


# Các trường match mà FLOW_MOD hỗ trợ. Khoá là tên khóa trong dict match của simulator.
FLOW_FIELDS = {
    "in_port": _Field(OXM_OF_IN_PORT, 4, _port_to_bytes, _bytes_to_port),
    "eth_dst": _Field(OXM_OF_ETH_DST, 6, _mac_to_bytes, _bytes_to_mac),
    "eth_src": _Field(OXM_OF_ETH_SRC, 6, _mac_to_bytes, _bytes_to_mac),
}
FLOW_FIELD_NAMES = {f.number: name for name, f in FLOW_FIELDS.items()}


def _pack_match(oxm: bytes) -> bytes:
    """Bọc các trường OXM thành ofp_match, đệm tới bội số của 8."""
    match_len = MATCH_HEADER.size + len(oxm)
    return MATCH_HEADER.pack(OFPMT_OXM, match_len) + oxm + b"\x00" * (-match_len % 8)


def _split_match(body: bytes, offset: int) -> tuple[bytes, int]:
    """Đọc ofp_match tại offset; trả về (các trường OXM, vị trí ngay sau phần đệm)."""
    if len(body) < offset + MATCH_HEADER.size:
        raise ValueError("thiếu header của match")
    match_type, match_len = MATCH_HEADER.unpack_from(body, offset)
    if match_type != OFPMT_OXM or match_len < MATCH_HEADER.size:
        raise ValueError("match không hợp lệ")
    end = offset + (match_len + 7) // 8 * 8
    if len(body) < end:
        raise ValueError("match bị cắt ngắn")
    return body[offset + MATCH_HEADER.size: offset + match_len], end


def _iter_oxm(fields: bytes):
    """Duyệt các trường OXM, trả về (class, số trường, có mask không, giá trị)."""
    pos = 0
    while pos < len(fields):
        if len(fields) - pos < OXM_HEADER.size:
            raise ValueError("trường OXM bị cắt ngắn")
        oxm_class, field_mask, length = OXM_HEADER.unpack_from(fields, pos)
        start = pos + OXM_HEADER.size
        value = fields[start: start + length]
        if len(value) < length:
            raise ValueError("giá trị OXM bị cắt ngắn")
        yield oxm_class, field_mask >> 1, bool(field_mask & 1), value
        pos = start + length


def _output_action(port: int) -> bytes:
    max_len = OFPCML_NO_BUFFER if port == OFPP_CONTROLLER else OFPCML_MAX
    return (ACTION_HEADER.pack(OFPAT_OUTPUT, ACTION_HEADER.size + ACTION_OUTPUT.size)
            + ACTION_OUTPUT.pack(port, max_len))


def _output_ports(actions: bytes) -> list[int]:
    """Đọc danh sách action, chỉ chấp nhận OUTPUT; trả về các cổng đích."""
    outputs = []
    pos = 0
    while pos < len(actions):
        if len(actions) - pos < ACTION_HEADER.size:
            raise ValueError("action bị cắt ngắn")
        action_type, action_len = ACTION_HEADER.unpack_from(actions, pos)
        if action_len < 8 or action_len % 8 or pos + action_len > len(actions):
            raise ValueError("độ dài action không hợp lệ")
        if action_type != OFPAT_OUTPUT:
            raise ValueError(f"action type {action_type} chưa được hỗ trợ")
        if action_len != ACTION_HEADER.size + ACTION_OUTPUT.size:
            raise ValueError("action OUTPUT có độ dài sai")
        port, _max_len = ACTION_OUTPUT.unpack_from(actions, pos + ACTION_HEADER.size)
        outputs.append(port)
        pos += action_len
    return outputs


# ---------------------------------------------------------------- PACKET_IN

def pack_packet_in(in_port: int, pkt: Packet, xid: int = 0,
                   reason: int = OFPR_NO_MATCH) -> Message:
    data = packet_to_bytes(pkt)
    oxm = OXM_HEADER.pack(OFPXMC_OPENFLOW_BASIC, OXM_OF_IN_PORT << 1, 4)
    oxm += struct.pack("!I", in_port)
    body = PACKET_IN_FIXED.pack(OFP_NO_BUFFER, len(data), reason, 0, 0)
    body += _pack_match(oxm) + b"\x00\x00" + data   # 2 byte đệm trước dữ liệu
    return Message(OFPT_PACKET_IN, xid=xid, body=body)


def parse_packet_in(msg: Message) -> tuple[int, Packet]:
    """Trả về (in_port, packet)."""
    if msg.type != OFPT_PACKET_IN:
        raise ValueError(f"không phải PACKET_IN (type={msg.type})")
    body = msg.body
    fields, end = _split_match(body, PACKET_IN_FIXED.size)
    if len(body) < end + 2:
        raise ValueError("PACKET_IN bị cắt ngắn")
    return _find_in_port(fields), bytes_to_packet(body[end + 2:])


def _find_in_port(fields: bytes) -> int:
    for oxm_class, field, masked, value in _iter_oxm(fields):
        if oxm_class == OFPXMC_OPENFLOW_BASIC and field == OXM_OF_IN_PORT:
            if masked or len(value) != 4:
                raise ValueError("trường IN_PORT không hợp lệ")
            return struct.unpack("!I", value)[0]
    raise ValueError("PACKET_IN không có trường IN_PORT")


# --------------------------------------------------------------- PACKET_OUT

def pack_packet_out(out_port: int | str, pkt: Packet, in_port: int | None = None,
                    xid: int = 0) -> Message:
    if out_port == "FLOOD":
        port = OFPP_FLOOD
    elif isinstance(out_port, int) and 0 < out_port <= OFPP_MAX:
        port = out_port
    else:
        raise ValueError(f"out_port không hợp lệ: {out_port!r}")
    action = _output_action(port)
    in_p = OFPP_CONTROLLER if in_port is None else in_port
    body = PACKET_OUT_FIXED.pack(OFP_NO_BUFFER, in_p, len(action)) + action
    body += packet_to_bytes(pkt)
    return Message(OFPT_PACKET_OUT, xid=xid, body=body)


def parse_packet_out(msg: Message) -> tuple[int | str, int | None, Packet]:
    """Trả về (out_port hoặc "FLOOD", in_port hoặc None, packet)."""
    if msg.type != OFPT_PACKET_OUT:
        raise ValueError(f"không phải PACKET_OUT (type={msg.type})")
    body = msg.body
    if len(body) < PACKET_OUT_FIXED.size:
        raise ValueError("PACKET_OUT bị cắt ngắn")
    buffer_id, in_p, actions_len = PACKET_OUT_FIXED.unpack_from(body)
    if buffer_id != OFP_NO_BUFFER:
        raise ValueError("PACKET_OUT dùng buffer_id: switch mô phỏng không có bộ đệm")
    actions_end = PACKET_OUT_FIXED.size + actions_len
    if len(body) < actions_end:
        raise ValueError("danh sách action bị cắt ngắn")

    outputs = _output_ports(body[PACKET_OUT_FIXED.size: actions_end])
    if len(outputs) != 1:
        raise ValueError(f"cần đúng 1 action OUTPUT, nhận {len(outputs)}")
    port = outputs[0]
    if port in (OFPP_FLOOD, OFPP_ALL):
        out_port: int | str = "FLOOD"
    elif 0 < port <= OFPP_MAX:
        out_port = port
    else:
        raise ValueError(f"cổng đặc biệt 0x{port:08x} chưa được hỗ trợ")
    in_port = None if in_p in (OFPP_CONTROLLER, OFPP_ANY) else in_p
    return out_port, in_port, bytes_to_packet(body[actions_end:])


# ----------------------------------------------------------------- FLOW_MOD

def _flow_port_to_wire(out_port: int | str) -> int:
    if out_port == "FLOOD":
        return OFPP_FLOOD
    if out_port == "CONTROLLER":
        return OFPP_CONTROLLER
    if isinstance(out_port, int) and 0 < out_port <= OFPP_MAX:
        return out_port
    raise ValueError(f"out_port không hợp lệ: {out_port!r}")


def _flow_port_from_wire(port: int) -> int | str:
    if port in (OFPP_FLOOD, OFPP_ALL):
        return "FLOOD"
    if port == OFPP_CONTROLLER:
        return "CONTROLLER"
    if 0 < port <= OFPP_MAX:
        return port
    raise ValueError(f"cổng đặc biệt 0x{port:08x} chưa được hỗ trợ")


def _timeout_to_wire(idle_timeout: float | None) -> int:
    if idle_timeout is None:
        return 0                                  # 0 nghĩa là không hết hạn
    if idle_timeout != int(idle_timeout) or not 1 <= idle_timeout <= 0xFFFF:
        raise ValueError(
            f"idle_timeout phải là số giây nguyên trong 1..65535, nhận {idle_timeout!r}")
    return int(idle_timeout)


def _match_to_oxm(match: dict) -> bytes:
    unknown = set(match) - set(FLOW_FIELDS)
    if unknown:
        raise ValueError(f"trường match chưa được hỗ trợ: {sorted(unknown)}")
    oxm = b""
    for name in sorted(match, key=lambda n: FLOW_FIELDS[n].number):   # thứ tự cố định
        fld = FLOW_FIELDS[name]
        oxm += OXM_HEADER.pack(OFPXMC_OPENFLOW_BASIC, fld.number << 1, fld.length)
        oxm += fld.encode(match[name])
    return oxm


def pack_flow_mod(match: dict, out_port: int | str, priority: int = 1,
                  idle_timeout: float | None = None, xid: int = 0) -> Message:
    """FLOW_MOD (ADD): khớp `match`, hành động duy nhất là OUTPUT ra `out_port`.

    out_port có thể là số cổng, "FLOOD" hoặc "CONTROLLER".
    match rỗng + "CONTROLLER" chính là flow table-miss mà controller thường cài khi kết nối.
    """
    if not 0 <= priority <= 0xFFFF:
        raise ValueError(f"priority ngoài khoảng 0..65535: {priority!r}")
    action = _output_action(_flow_port_to_wire(out_port))
    instruction = INSTRUCTION_HEADER.pack(OFPIT_APPLY_ACTIONS,
                                          INSTRUCTION_HEADER.size + len(action)) + action
    body = FLOW_MOD_FIXED.pack(
        0, 0, 0, OFPFC_ADD, _timeout_to_wire(idle_timeout), 0, priority,
        OFP_NO_BUFFER, OFPP_ANY, OFPG_ANY, 0,
    )
    body += _pack_match(_match_to_oxm(match)) + instruction
    return Message(OFPT_FLOW_MOD, xid=xid, body=body)


def parse_flow_mod(msg: Message) -> FlowSpec:
    """Trả về FlowSpec(match, out_port, priority, idle_timeout)."""
    if msg.type != OFPT_FLOW_MOD:
        raise ValueError(f"không phải FLOW_MOD (type={msg.type})")
    body = msg.body
    if len(body) < FLOW_MOD_FIXED.size:
        raise ValueError("FLOW_MOD bị cắt ngắn")
    (_cookie, _cookie_mask, table_id, command, idle, hard, priority,
     buffer_id, _out_port, _out_group, _flags) = FLOW_MOD_FIXED.unpack_from(body)
    if command != OFPFC_ADD:
        raise ValueError(f"FLOW_MOD command {command} chưa được hỗ trợ (chỉ ADD)")
    if table_id != 0:
        raise ValueError(f"chỉ có table 0, nhận table_id={table_id}")
    if hard:
        raise ValueError("hard_timeout chưa được hỗ trợ")
    if buffer_id != OFP_NO_BUFFER:
        raise ValueError("FLOW_MOD dùng buffer_id: switch mô phỏng không có bộ đệm")

    fields, end = _split_match(body, FLOW_MOD_FIXED.size)
    match = {}
    for oxm_class, field, masked, value in _iter_oxm(fields):
        name = FLOW_FIELD_NAMES.get(field) if oxm_class == OFPXMC_OPENFLOW_BASIC else None
        if name is None:
            raise ValueError(
                f"trường match (class=0x{oxm_class:04x}, field={field}) chưa được hỗ trợ")
        fld = FLOW_FIELDS[name]
        if masked or len(value) != fld.length:
            raise ValueError(f"trường match {name} không hợp lệ (có mask hoặc sai độ dài)")
        match[name] = fld.decode(value)

    ports: list[int] = []
    pos = end
    while pos < len(body):
        if len(body) - pos < INSTRUCTION_HEADER.size:
            raise ValueError("instruction bị cắt ngắn")
        itype, ilen = INSTRUCTION_HEADER.unpack_from(body, pos)
        if ilen < INSTRUCTION_HEADER.size or ilen % 8 or pos + ilen > len(body):
            raise ValueError("độ dài instruction không hợp lệ")
        if itype != OFPIT_APPLY_ACTIONS:
            raise ValueError(f"instruction type {itype} chưa được hỗ trợ")
        ports += _output_ports(body[pos + INSTRUCTION_HEADER.size: pos + ilen])
        pos += ilen
    if len(ports) != 1:
        raise ValueError(f"cần đúng 1 action OUTPUT, nhận {len(ports)}")

    return FlowSpec(match, _flow_port_from_wire(ports[0]), priority,
                    float(idle) if idle else None)


# -------------------------------------------------------------------- ERROR

def pack_error(err_type: int, code: int, request: Message) -> Message:
    """ERROR trả lời `request`: cùng xid, kèm tối đa 64 byte đầu của thông điệp gây lỗi."""
    data = pack_message(request)[:ERROR_DATA_BYTES]
    return Message(OFPT_ERROR, xid=request.xid, body=struct.pack("!HH", err_type, code) + data)


def parse_error(msg: Message) -> tuple[int, int, bytes]:
    """Trả về (loại lỗi, mã lỗi, dữ liệu kèm theo)."""
    if msg.type != OFPT_ERROR:
        raise ValueError(f"không phải ERROR (type={msg.type})")
    if len(msg.body) < 4:
        raise ValueError("ERROR bị cắt ngắn")
    err_type, code = struct.unpack_from("!HH", msg.body)
    return err_type, code, msg.body[4:]


# ---------------------------------------------------------------- MULTIPART

class PortDesc(NamedTuple):
    port_no: int
    mac: str
    name: str


def parse_multipart_request(msg: Message) -> tuple[int, int, bytes]:
    """Trả về (loại multipart, cờ, phần thân)."""
    if msg.type != OFPT_MULTIPART_REQUEST:
        raise ValueError(f"không phải MULTIPART_REQUEST (type={msg.type})")
    if len(msg.body) < MULTIPART_HEADER.size:
        raise ValueError("MULTIPART_REQUEST bị cắt ngắn")
    mp_type, flags = MULTIPART_HEADER.unpack_from(msg.body)
    return mp_type, flags, msg.body[MULTIPART_HEADER.size:]


def pack_port_desc_reply(ports: list[PortDesc], xid: int = 0) -> Message:
    """MULTIPART_REPLY loại PORT_DESC: mỗi cổng là một ofp_port dài 64 byte.

    Cổng được báo là 1 Gb/s full duplex, đang hoạt động; không mô phỏng trạng thái liên kết.
    """
    body = MULTIPART_HEADER.pack(OFPMP_PORT_DESC, 0)        # cờ 0: không còn phần nào nữa
    for port in ports:
        if not isinstance(port.port_no, int) or not 0 < port.port_no <= OFPP_MAX:
            raise ValueError(f"số cổng không hợp lệ: {port.port_no!r}")
        name = port.name.encode("ascii")
        if len(name) > 15:
            raise ValueError(f"tên cổng dài quá 15 ký tự: {port.name!r}")
        body += PORT_DESC_ENTRY.pack(
            port.port_no, _mac_to_bytes(port.mac), name,
            0, 0,                                            # config, state
            OFPPF_1GB_FD, 0, OFPPF_1GB_FD, 0,                # curr, advertised, supported, peer
            1_000_000, 1_000_000,                            # curr_speed, max_speed (kb/s)
        )
    return Message(OFPT_MULTIPART_REPLY, xid=xid, body=body)