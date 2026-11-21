"""Đóng gói / giải gói một tập con OpenFlow 1.3 cho switch mô phỏng.

Phạm vi cố ý hẹp:
  - PACKET_IN: luôn gửi kèm đủ dữ liệu (buffer_id = NO_BUFFER), match chỉ có IN_PORT.
    Khi đọc, các trường OXM khác được bỏ qua, IN_PORT là bắt buộc.
  - PACKET_OUT: đúng một action OUTPUT, không dùng buffer, kèm dữ liệu gói.
    FLOOD và ALL đều đọc thành "FLOOD" (switch mô phỏng loại trừ cổng vào).
  - FLOW_MOD: command ADD, một bảng (table 0), match gồm in_port/eth_dst/eth_src,
    action OUTPUT hoặc GROUP, và tùy chọn một meter instruction.
    Khi đọc, trường match lạ bị TỪ CHỐI (không bỏ qua như PACKET_IN), vì bỏ qua sẽ
    làm flow khớp rộng hơn controller định. Cờ (flags) được bỏ qua; cookie được lưu.
  - GROUP: loại ALL/INDIRECT với bucket OUTPUT. METER: một band DROP theo KBPS.
"""

import math
import struct
from typing import Callable, NamedTuple

from sdnsim.core.packet import Packet

from .frame import bytes_to_packet, packet_to_bytes
from .header import (
    HEADER_LEN, OFPT_FLOW_MOD, OFPT_GROUP_MOD, OFPT_METER_MOD, OFPT_PACKET_IN,
    OFPT_PACKET_OUT, Message, pack_message,
)

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
OFPFC_MODIFY = 1
OFPFC_DELETE = 3
OFPIT_APPLY_ACTIONS = 4
OFPIT_METER = 6
OFPAT_GROUP = 22

OFPGC_ADD = 0
OFPGC_MODIFY = 1
OFPGC_DELETE = 2
OFPGT_ALL = 0
OFPGT_INDIRECT = 2
OFPG_MAX = 0xFFFFFF00

OFPMC_ADD = 0
OFPMC_MODIFY = 1
OFPMC_DELETE = 2
OFPMF_KBPS = 1
OFPMF_BURST = 4
OFPMF_STATS = 8
OFPMF_SUPPORTED = OFPMF_KBPS | OFPMF_BURST | OFPMF_STATS
OFPMBT_DROP = 1
OFPM_MAX = 0xFFFF0000

# Thông điệp ngoài header.py
OFPT_ERROR = 1
OFPT_SET_CONFIG = 9
OFPT_BARRIER_REQUEST = 20
OFPT_BARRIER_REPLY = 21
OFPT_MULTIPART_REQUEST = 18
OFPT_MULTIPART_REPLY = 19
OFPMP_DESC = 0                   # mô tả switch
OFPMP_FLOW = 1                  # thống kê flow
OFPMP_AGGREGATE = 2             # thống kê tổng hợp flow
OFPMP_TABLE = 3                 # thống kê bảng flow
OFPMP_PORT_STATS = 4            # thống kê cổng
OFPMP_GROUP = 6                 # thống kê group
OFPMP_GROUP_DESC = 7
OFPMP_METER = 9                 # thống kê meter
OFPMP_METER_CONFIG = 10
OFPMP_PORT_DESC = 13            # mô tả cổng
OFPMPF_REPLY_MORE = 1           # cờ: còn thông điệp trả lời tiếp theo cùng xid
OFPTT_ALL = 0xFF                # "mọi bảng" trong yêu cầu thống kê flow
OFPG_ALL = 0xFFFFFFFC
OFPM_ALL = 0xFFFFFFFF
MAX_MESSAGE_BYTES = 0xFFFF      # độ dài tối đa của một thông điệp OpenFlow
OFPPF_1GB_FD = 1 << 5
OFPPF_10MB_FD = 1 << 1
OFPPF_100MB_FD = 1 << 3
OFPPF_10GB_FD = 1 << 7
OFPPF_40GB_FD = 1 << 9
OFPPF_100GB_FD = 1 << 11
OFPPF_1TB_FD = 1 << 13
OFPPF_OTHER = 1 << 14
_PORT_FEATURES_BY_BPS = {
    10_000_000: OFPPF_10MB_FD,
    100_000_000: OFPPF_100MB_FD,
    1_000_000_000: OFPPF_1GB_FD,
    10_000_000_000: OFPPF_10GB_FD,
    40_000_000_000: OFPPF_40GB_FD,
    100_000_000_000: OFPPF_100GB_FD,
    1_000_000_000_000: OFPPF_1TB_FD,
}

# Loại và mã lỗi của ERROR (đã đối chiếu với os-ken)
OFPET_BAD_REQUEST = 1
OFPET_BAD_ACTION = 2
OFPET_FLOW_MOD_FAILED = 5
OFPBRC_BAD_TYPE = 1             # thông điệp không được hỗ trợ
OFPBRC_BAD_MULTIPART = 2        # loại multipart không được hỗ trợ
OFPBRC_BAD_LEN = 6              # độ dài thông điệp sai
OFPBRC_BAD_PORT = 11            # cổng không tồn tại
OFPET_BAD_MATCH = 4
OFPBMC_BAD_FIELD = 6            # trường match không được hỗ trợ
OFPBAC_BAD_TYPE = 0             # action không được hỗ trợ
OFPBAC_BAD_OUT_PORT = 4         # cổng đích không tồn tại
OFPET_GROUP_MOD_FAILED = 6
OFPET_METER_MOD_FAILED = 10
OFPGMFC_GROUP_EXISTS = 0
OFPGMFC_INVALID_GROUP = 1
OFPGMFC_GROUP_IN_USE = 2
OFPGMFC_UNKNOWN_GROUP = 7
OFPGMFC_BAD_TYPE = 9
OFPGMFC_BAD_COMMAND = 10
OFPGMFC_BAD_BUCKET = 11
OFPMMFC_METER_EXISTS = 1
OFPMMFC_INVALID_METER = 2
OFPMMFC_UNKNOWN_METER = 3
OFPMMFC_METER_IN_USE = 4
OFPMMFC_UNSUPPORTED = 5
OFPMMFC_BAD_BAND = 6
OFPMMFC_BAD_FLAGS = 11
OFPMMFC_BAD_COMMAND = 14
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
INSTRUCTION_METER = struct.Struct("!HHI")
ACTION_GROUP = struct.Struct("!HHI")
GROUP_MOD_FIXED = struct.Struct("!HBxI")
GROUP_BUCKET_FIXED = struct.Struct("!HHII4x")
METER_MOD_FIXED = struct.Struct("!HHI")
METER_BAND_DROP = struct.Struct("!HHII4x")
PORT_STATS_REQUEST = struct.Struct("!I4x")    # port_no (OFPP_ANY = mọi cổng), đệm 4
PORT_STATS_ENTRY = struct.Struct("!I4x12Q2I")  # port_no, đệm 4, 12 bộ đếm, duration_sec/nsec (112 byte)
DESC_STATS = struct.Struct("!256s256s256s32s256s")
TABLE_STATS_ENTRY = struct.Struct("!B3xIQQ")
AGGREGATE_STATS_ENTRY = struct.Struct("!QQI4x")
GROUP_STATS_REQUEST = struct.Struct("!I4x")
METER_STATS_REQUEST = struct.Struct("!I4x")
GROUP_STATS_FIXED = struct.Struct("!H2xII4xQQII")
GROUP_DESC_FIXED = struct.Struct("!HBxI")
METER_STATS_FIXED = struct.Struct("!IH6xIQQII")
METER_CONFIG_FIXED = struct.Struct("!HHI")
BUCKET_COUNTER = struct.Struct("!QQ")
METER_BAND_STATS = struct.Struct("!QQ")
# table_id, đệm 3, out_port, out_group, đệm 4, cookie, cookie_mask  (32 byte, rồi tới match)
FLOW_STATS_REQUEST_FIXED = struct.Struct("!B3xII4xQQ")
# length, table_id, đệm 1, duration_sec, duration_nsec, priority, idle_timeout, hard_timeout,
# flags, đệm 4, cookie, packet_count, byte_count  (48 byte, rồi tới match và instruction)
FLOW_STATS_ENTRY_FIXED = struct.Struct("!HBxIIHHHH4xQQQ")


class FlowSpec(NamedTuple):
    match: dict
    out_port: int | str | None
    priority: int
    idle_timeout: float | None
    group_id: int | None = None
    meter_id: int | None = None


class GroupBucket(NamedTuple):
    out_port: int
    weight: int = 0
    watch_port: int = OFPP_ANY
    watch_group: int = OFPG_ANY


class GroupModSpec(NamedTuple):
    command: int
    group_type: int
    group_id: int
    buckets: list[GroupBucket]


class GroupDesc(NamedTuple):
    group_type: int
    group_id: int
    buckets: list[GroupBucket]


class MeterModSpec(NamedTuple):
    command: int
    flags: int
    meter_id: int
    rate: int | None
    burst_size: int


class MeterConfig(NamedTuple):
    flags: int
    meter_id: int
    rate: int
    burst_size: int


class BucketStats(NamedTuple):
    packet_count: int
    byte_count: int


class GroupStats(NamedTuple):
    group_id: int
    ref_count: int
    packet_count: int
    byte_count: int
    duration_sec: int
    duration_nsec: int
    bucket_stats: list[BucketStats]


class MeterStats(NamedTuple):
    meter_id: int
    flow_count: int
    packet_in_count: int
    byte_in_count: int
    duration_sec: int
    duration_nsec: int
    band_stats: list[BucketStats]


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


def _flow_action(actions: bytes) -> tuple[int | None, int | None]:
    if len(actions) < ACTION_HEADER.size:
        raise ValueError("thiếu action cho flow")
    action_type, action_len = ACTION_HEADER.unpack_from(actions)
    if action_len != len(actions):
        raise ValueError("flow phải có đúng một action")
    if action_type == OFPAT_OUTPUT:
        outputs = _output_ports(actions)
        if len(outputs) != 1:
            raise ValueError("flow phải có đúng một action OUTPUT")
        return _flow_port_from_wire(outputs[0]), None
    if action_type == OFPAT_GROUP:
        if action_len != ACTION_GROUP.size:
            raise ValueError("action GROUP có độ dài sai")
        return None, ACTION_GROUP.unpack(actions)[2]
    raise ValueError(f"action type {action_type} chưa được hỗ trợ")


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

def _parse_match_fields(fields: bytes) -> dict:
    """Đọc các trường OXM của một match thành dict; trường lạ hoặc có mask bị từ chối."""
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
    return match


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


def pack_flow_mod(match: dict, out_port: int | str | None, priority: int = 1,
                  idle_timeout: float | None = None, xid: int = 0,
                  cookie: int = 0, group_id: int | None = None,
                  meter_id: int | None = None) -> Message:
    """FLOW_MOD (ADD): OUTPUT ra cổng hoặc GROUP, tùy chọn kèm meter.

    out_port có thể là số cổng, "FLOOD" hoặc "CONTROLLER"; group_id thay thế out_port.
    match rỗng + "CONTROLLER" chính là flow table-miss mà controller thường cài khi kết nối.
    """
    if not 0 <= priority <= 0xFFFF:
        raise ValueError(f"priority ngoài khoảng 0..65535: {priority!r}")
    if not isinstance(cookie, int) or isinstance(cookie, bool) or not 0 <= cookie <= 0xFFFFFFFFFFFFFFFF:
        raise ValueError(f"cookie ngoài khoảng 0..2^64-1: {cookie!r}")
    if (group_id is None) == (out_port is None):
        raise ValueError("flow cần đúng một trong out_port hoặc group_id")
    if group_id is not None:
        if not 0 <= group_id < OFPG_MAX:
            raise ValueError(f"group_id không hợp lệ: {group_id!r}")
        action = ACTION_GROUP.pack(OFPAT_GROUP, ACTION_GROUP.size, group_id)
    else:
        action = _output_action(_flow_port_to_wire(out_port))
    instruction = INSTRUCTION_HEADER.pack(OFPIT_APPLY_ACTIONS,
                                          INSTRUCTION_HEADER.size + len(action)) + action
    body = FLOW_MOD_FIXED.pack(
        cookie, 0, 0, OFPFC_ADD, _timeout_to_wire(idle_timeout), 0, priority,
        OFP_NO_BUFFER, OFPP_ANY, OFPG_ANY, 0,
    )
    meter_instruction = b""
    if meter_id is not None:
        if not 0 <= meter_id < OFPM_MAX:
            raise ValueError(f"meter_id không hợp lệ: {meter_id!r}")
        meter_instruction = INSTRUCTION_METER.pack(OFPIT_METER, INSTRUCTION_METER.size, meter_id)
    body += _pack_match(_match_to_oxm(match)) + meter_instruction + instruction
    return Message(OFPT_FLOW_MOD, xid=xid, body=body)


def parse_flow_mod(msg: Message) -> FlowSpec:
    """Trả về match, output hoặc group, priority, timeout và meter của FLOW_MOD."""
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
    match = _parse_match_fields(fields)

    out_port = group_id = meter_id = None
    action_count = meter_count = 0
    pos = end
    while pos < len(body):
        if len(body) - pos < INSTRUCTION_HEADER.size:
            raise ValueError("instruction bị cắt ngắn")
        itype, ilen = INSTRUCTION_HEADER.unpack_from(body, pos)
        if ilen < INSTRUCTION_HEADER.size or ilen % 8 or pos + ilen > len(body):
            raise ValueError("độ dài instruction không hợp lệ")
        if itype == OFPIT_METER:
            if ilen != INSTRUCTION_METER.size or meter_count:
                raise ValueError("instruction METER không hợp lệ hoặc lặp")
            meter_id = INSTRUCTION_METER.unpack_from(body, pos)[2]
            meter_count += 1
        elif itype == OFPIT_APPLY_ACTIONS:
            if action_count:
                raise ValueError("instruction APPLY_ACTIONS bị lặp")
            out_port, group_id = _flow_action(body[pos + INSTRUCTION_HEADER.size: pos + ilen])
            action_count += 1
        else:
            raise ValueError(f"instruction type {itype} chưa được hỗ trợ")
        pos += ilen
    if action_count != 1:
        raise ValueError("flow cần đúng một instruction APPLY_ACTIONS")

    return FlowSpec(match, out_port, priority, float(idle) if idle else None,
                    group_id, meter_id)


def parse_flow_mod_cookie(msg: Message) -> int:
    if msg.type != OFPT_FLOW_MOD:
        raise ValueError(f"không phải FLOW_MOD (type={msg.type})")
    if len(msg.body) < FLOW_MOD_FIXED.size:
        raise ValueError("FLOW_MOD bị cắt ngắn")
    return FLOW_MOD_FIXED.unpack_from(msg.body)[0]


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
    bandwidth: float | None = None


def _port_speed_fields(bandwidth: float | None) -> tuple[int, int]:
    if bandwidth is None:
        return 0, 0
    if (not isinstance(bandwidth, (int, float)) or isinstance(bandwidth, bool)
            or bandwidth <= 0 or bandwidth > 0xFFFFFFFF * 1000
            or not math.isfinite(bandwidth)):
        raise ValueError(f"băng thông của cổng không hợp lệ: {bandwidth!r}")
    feature = _PORT_FEATURES_BY_BPS.get(round(bandwidth), OFPPF_OTHER)
    speed_kbps = max(1, round(bandwidth / 1000))
    return feature, speed_kbps


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
        curr, speed_kbps = _port_speed_fields(port.bandwidth)
        body += PORT_DESC_ENTRY.pack(
            port.port_no, _mac_to_bytes(port.mac), name,
            0, 0,                                            # config, state
            curr, 0, curr, 0,                               # curr, advertised, supported, peer
            speed_kbps, speed_kbps,                         # curr_speed, max_speed (kb/s)
        )
    return Message(OFPT_MULTIPART_REPLY, xid=xid, body=body)


# ------------------------------------------------------------ MULTIPART: thống kê

def pack_desc_stats_reply(name: str, dpid: int, xid: int = 0) -> list[Message]:
    body = DESC_STATS.pack(
        b"SDN Emulator", b"Simulated switch", b"SDN-Emulator",
        str(dpid).encode("ascii"), f"{name} (dpid={dpid})".encode("utf-8"),
    )
    return _multipart_replies(OFPMP_DESC, [body], xid)


class PortStats(NamedTuple):
    """Thứ tự trường đúng bằng thứ tự trên đường truyền của ofp_port_stats (OpenFlow 1.3)."""
    port_no: int
    rx_packets: int = 0
    tx_packets: int = 0
    rx_bytes: int = 0
    tx_bytes: int = 0
    rx_dropped: int = 0
    tx_dropped: int = 0
    rx_errors: int = 0
    tx_errors: int = 0
    rx_frame_err: int = 0
    rx_over_err: int = 0
    rx_crc_err: int = 0
    collisions: int = 0
    duration_sec: int = 0
    duration_nsec: int = 0


class TableStats(NamedTuple):
    table_id: int
    active_count: int
    lookup_count: int
    matched_count: int


class AggregateStats(NamedTuple):
    packet_count: int
    byte_count: int
    flow_count: int


class FlowStats(NamedTuple):
    match: dict
    out_port: int | str | None
    priority: int
    idle_timeout: float | None
    packet_count: int
    byte_count: int
    duration_sec: int = 0
    duration_nsec: int = 0
    cookie: int = 0
    group_id: int | None = None
    meter_id: int | None = None


class FlowStatsRequest(NamedTuple):
    table_id: int                # 0, hoặc OFPTT_ALL (255) cho mọi bảng
    out_port: int | str | None   # None = không lọc theo cổng ra
    match: dict                  # flow phải thỏa các trường được yêu cầu; được phép có thêm trường
    cookie: int = 0
    cookie_mask: int = 0
    out_group: int | None = None


def _multipart_replies(mp_type: int, entries: list[bytes], xid: int) -> list[Message]:
    """Gói các mục thành MULTIPART_REPLY, mỗi thông điệp không vượt 65535 byte.

    Mọi thông điệp trừ cái cuối mang cờ REPLY_MORE và cùng xid. Luôn trả về ít nhất một
    thông điệp, kể cả khi không có mục nào.
    """
    limit = MAX_MESSAGE_BYTES - HEADER_LEN - MULTIPART_HEADER.size
    chunks: list[list[bytes]] = [[]]
    size = 0
    for entry in entries:
        if len(entry) > limit:
            raise ValueError("một mục thống kê dài quá một thông điệp OpenFlow")
        if size + len(entry) > limit:
            chunks.append([])
            size = 0
        chunks[-1].append(entry)
        size += len(entry)
    return [
        Message(OFPT_MULTIPART_REPLY, xid=xid,
                body=MULTIPART_HEADER.pack(mp_type, OFPMPF_REPLY_MORE if i < len(chunks) - 1 else 0)
                + b"".join(chunk))
        for i, chunk in enumerate(chunks)
    ]


def parse_multipart_reply(msg: Message) -> tuple[int, int, bytes]:
    """Trả về (loại multipart, cờ, phần thân)."""
    if msg.type != OFPT_MULTIPART_REPLY:
        raise ValueError(f"không phải MULTIPART_REPLY (type={msg.type})")
    if len(msg.body) < MULTIPART_HEADER.size:
        raise ValueError("MULTIPART_REPLY bị cắt ngắn")
    mp_type, flags = MULTIPART_HEADER.unpack_from(msg.body)
    return mp_type, flags, msg.body[MULTIPART_HEADER.size:]


def pack_table_stats_reply(stats: list[TableStats], xid: int = 0) -> list[Message]:
    entries = [TABLE_STATS_ENTRY.pack(*entry) for entry in stats]
    return _multipart_replies(OFPMP_TABLE, entries, xid)


def pack_aggregate_stats_reply(stats: AggregateStats, xid: int = 0) -> list[Message]:
    return _multipart_replies(OFPMP_AGGREGATE, [AGGREGATE_STATS_ENTRY.pack(*stats)], xid)


def parse_aggregate_stats_reply(msg: Message) -> AggregateStats:
    mp_type, _flags, body = parse_multipart_reply(msg)
    if mp_type != OFPMP_AGGREGATE:
        raise ValueError(f"không phải trả lời AGGREGATE (multipart type={mp_type})")
    if len(body) != AGGREGATE_STATS_ENTRY.size:
        raise ValueError("độ dài AGGREGATE không hợp lệ")
    return AggregateStats(*AGGREGATE_STATS_ENTRY.unpack(body))


def _pack_group_bucket(bucket: GroupBucket) -> bytes:
    if not 0 < bucket.out_port <= OFPP_MAX:
        raise ValueError(f"group bucket chỉ hỗ trợ cổng vật lý: {bucket.out_port!r}")
    action = _output_action(bucket.out_port)
    length = GROUP_BUCKET_FIXED.size + len(action)
    return GROUP_BUCKET_FIXED.pack(
        length, bucket.weight, bucket.watch_port, bucket.watch_group) + action


def _parse_group_buckets(body: bytes) -> list[GroupBucket]:
    buckets = []
    pos = 0
    while pos < len(body):
        if len(body) - pos < GROUP_BUCKET_FIXED.size:
            raise ValueError("group bucket bị cắt ngắn")
        length, weight, watch_port, watch_group = GROUP_BUCKET_FIXED.unpack_from(body, pos)
        if length < GROUP_BUCKET_FIXED.size or length % 8 or pos + length > len(body):
            raise ValueError("độ dài group bucket không hợp lệ")
        actions = body[pos + GROUP_BUCKET_FIXED.size:pos + length]
        outputs = _output_ports(actions)
        if len(outputs) != 1 or not 0 < outputs[0] <= OFPP_MAX:
            raise ValueError("group bucket cần đúng một action OUTPUT tới cổng vật lý")
        buckets.append(GroupBucket(outputs[0], weight, watch_port, watch_group))
        pos += length
    return buckets


def pack_group_mod(command: int, group_type: int, group_id: int,
                   buckets: list[GroupBucket] | None = None, xid: int = 0) -> Message:
    body = GROUP_MOD_FIXED.pack(command, group_type, group_id)
    body += b"".join(_pack_group_bucket(bucket) for bucket in buckets or [])
    return Message(OFPT_GROUP_MOD, xid=xid, body=body)


def parse_group_mod(msg: Message) -> GroupModSpec:
    if msg.type != OFPT_GROUP_MOD or len(msg.body) < GROUP_MOD_FIXED.size:
        raise ValueError("GROUP_MOD không hợp lệ hoặc bị cắt ngắn")
    command, group_type, group_id = GROUP_MOD_FIXED.unpack_from(msg.body)
    buckets = _parse_group_buckets(msg.body[GROUP_MOD_FIXED.size:])
    return GroupModSpec(command, group_type, group_id, buckets)


def pack_meter_mod(command: int, flags: int, meter_id: int, rate: int | None = None,
                   burst_size: int = 0, xid: int = 0) -> Message:
    body = METER_MOD_FIXED.pack(command, flags, meter_id)
    if rate is not None:
        body += METER_BAND_DROP.pack(OFPMBT_DROP, METER_BAND_DROP.size, rate, burst_size)
    return Message(OFPT_METER_MOD, xid=xid, body=body)


def parse_meter_mod(msg: Message) -> MeterModSpec:
    if msg.type != OFPT_METER_MOD or len(msg.body) < METER_MOD_FIXED.size:
        raise ValueError("METER_MOD không hợp lệ hoặc bị cắt ngắn")
    command, flags, meter_id = METER_MOD_FIXED.unpack_from(msg.body)
    bands = msg.body[METER_MOD_FIXED.size:]
    if not bands:
        return MeterModSpec(command, flags, meter_id, None, 0)
    if len(bands) != METER_BAND_DROP.size:
        raise ValueError("chỉ hỗ trợ một band METER DROP")
    band_type, band_len, rate, burst_size = METER_BAND_DROP.unpack(bands)
    if band_type != OFPMBT_DROP or band_len != METER_BAND_DROP.size:
        raise ValueError("chỉ hỗ trợ band METER DROP hợp lệ")
    return MeterModSpec(command, flags, meter_id, rate, burst_size)


def pack_group_stats_reply(stats: list[GroupStats], xid: int = 0) -> list[Message]:
    entries = []
    for stat in stats:
        length = GROUP_STATS_FIXED.size + len(stat.bucket_stats) * BUCKET_COUNTER.size
        entry = GROUP_STATS_FIXED.pack(
            length, stat.group_id, stat.ref_count, stat.packet_count, stat.byte_count,
            stat.duration_sec, stat.duration_nsec)
        entry += b"".join(BUCKET_COUNTER.pack(*bucket) for bucket in stat.bucket_stats)
        entries.append(entry)
    return _multipart_replies(OFPMP_GROUP, entries, xid)


def parse_group_stats_reply(msg: Message) -> list[GroupStats]:
    mp_type, _flags, body = parse_multipart_reply(msg)
    if mp_type != OFPMP_GROUP:
        raise ValueError(f"không phải trả lời GROUP (multipart type={mp_type})")
    stats = []
    pos = 0
    while pos < len(body):
        if len(body) - pos < GROUP_STATS_FIXED.size:
            raise ValueError("mục GROUP_STATS bị cắt ngắn")
        length, group_id, ref_count, packets, nbytes, sec, nsec = (
            GROUP_STATS_FIXED.unpack_from(body, pos))
        if (length < GROUP_STATS_FIXED.size or length % 8 or pos + length > len(body)
                or (length - GROUP_STATS_FIXED.size) % BUCKET_COUNTER.size):
            raise ValueError("độ dài mục GROUP_STATS không hợp lệ")
        buckets = [
            BucketStats(*BUCKET_COUNTER.unpack_from(body, offset))
            for offset in range(pos + GROUP_STATS_FIXED.size, pos + length, BUCKET_COUNTER.size)
        ]
        stats.append(GroupStats(group_id, ref_count, packets, nbytes, sec, nsec, buckets))
        pos += length
    return stats


def pack_group_desc_reply(groups: list[GroupDesc], xid: int = 0) -> list[Message]:
    entries = []
    for group in groups:
        buckets = b"".join(_pack_group_bucket(bucket) for bucket in group.buckets)
        length = GROUP_DESC_FIXED.size + len(buckets)
        entries.append(GROUP_DESC_FIXED.pack(length, group.group_type, group.group_id) + buckets)
    return _multipart_replies(OFPMP_GROUP_DESC, entries, xid)


def parse_group_desc_reply(msg: Message) -> list[GroupDesc]:
    mp_type, _flags, body = parse_multipart_reply(msg)
    if mp_type != OFPMP_GROUP_DESC:
        raise ValueError(f"không phải trả lời GROUP_DESC (multipart type={mp_type})")
    groups = []
    pos = 0
    while pos < len(body):
        if len(body) - pos < GROUP_DESC_FIXED.size:
            raise ValueError("mục GROUP_DESC bị cắt ngắn")
        length, group_type, group_id = GROUP_DESC_FIXED.unpack_from(body, pos)
        if length < GROUP_DESC_FIXED.size or length % 8 or pos + length > len(body):
            raise ValueError("độ dài mục GROUP_DESC không hợp lệ")
        buckets = _parse_group_buckets(
            body[pos + GROUP_DESC_FIXED.size:pos + length])
        groups.append(GroupDesc(group_type, group_id, buckets))
        pos += length
    return groups


def pack_meter_stats_reply(stats: list[MeterStats], xid: int = 0) -> list[Message]:
    entries = []
    for stat in stats:
        length = METER_STATS_FIXED.size + len(stat.band_stats) * METER_BAND_STATS.size
        entry = METER_STATS_FIXED.pack(
            stat.meter_id, length, stat.flow_count, stat.packet_in_count, stat.byte_in_count,
            stat.duration_sec, stat.duration_nsec)
        entry += b"".join(METER_BAND_STATS.pack(*band) for band in stat.band_stats)
        entries.append(entry)
    return _multipart_replies(OFPMP_METER, entries, xid)


def parse_meter_stats_reply(msg: Message) -> list[MeterStats]:
    mp_type, _flags, body = parse_multipart_reply(msg)
    if mp_type != OFPMP_METER:
        raise ValueError(f"không phải trả lời METER (multipart type={mp_type})")
    stats = []
    pos = 0
    while pos < len(body):
        if len(body) - pos < METER_STATS_FIXED.size:
            raise ValueError("mục METER_STATS bị cắt ngắn")
        meter_id, length, flow_count, packets, nbytes, sec, nsec = (
            METER_STATS_FIXED.unpack_from(body, pos))
        if (length < METER_STATS_FIXED.size or length % 8 or pos + length > len(body)
                or (length - METER_STATS_FIXED.size) % METER_BAND_STATS.size):
            raise ValueError("độ dài mục METER_STATS không hợp lệ")
        bands = [
            BucketStats(*METER_BAND_STATS.unpack_from(body, offset))
            for offset in range(pos + METER_STATS_FIXED.size, pos + length,
                                METER_BAND_STATS.size)
        ]
        stats.append(MeterStats(
            meter_id, flow_count, packets, nbytes, sec, nsec, bands))
        pos += length
    return stats


def pack_meter_config_reply(configs: list[MeterConfig], xid: int = 0) -> list[Message]:
    entries = []
    for config in configs:
        band = METER_BAND_DROP.pack(
            OFPMBT_DROP, METER_BAND_DROP.size, config.rate, config.burst_size)
        length = METER_CONFIG_FIXED.size + len(band)
        entries.append(METER_CONFIG_FIXED.pack(length, config.flags, config.meter_id) + band)
    return _multipart_replies(OFPMP_METER_CONFIG, entries, xid)


def parse_meter_config_reply(msg: Message) -> list[MeterConfig]:
    mp_type, _flags, body = parse_multipart_reply(msg)
    if mp_type != OFPMP_METER_CONFIG:
        raise ValueError(f"không phải trả lời METER_CONFIG (multipart type={mp_type})")
    configs = []
    pos = 0
    while pos < len(body):
        if len(body) - pos < METER_CONFIG_FIXED.size:
            raise ValueError("mục METER_CONFIG bị cắt ngắn")
        length, flags, meter_id = METER_CONFIG_FIXED.unpack_from(body, pos)
        if length != METER_CONFIG_FIXED.size + METER_BAND_DROP.size or pos + length > len(body):
            raise ValueError("độ dài mục METER_CONFIG không hợp lệ")
        band_type, band_len, rate, burst_size = METER_BAND_DROP.unpack_from(
            body, pos + METER_CONFIG_FIXED.size)
        if band_type != OFPMBT_DROP or band_len != METER_BAND_DROP.size:
            raise ValueError("band METER_CONFIG không hợp lệ")
        configs.append(MeterConfig(flags, meter_id, rate, burst_size))
        pos += length
    return configs


def parse_group_stats_request(body: bytes) -> int:
    if len(body) != GROUP_STATS_REQUEST.size:
        raise ValueError("yêu cầu GROUP_STATS có độ dài sai")
    return GROUP_STATS_REQUEST.unpack(body)[0]


def parse_meter_stats_request(body: bytes) -> int:
    if len(body) != METER_STATS_REQUEST.size:
        raise ValueError("yêu cầu METER_STATS có độ dài sai")
    return METER_STATS_REQUEST.unpack(body)[0]


def _pack_id_stats_request(mp_type: int, object_id: int, xid: int) -> Message:
    body = MULTIPART_HEADER.pack(mp_type, 0) + GROUP_STATS_REQUEST.pack(object_id)
    return Message(OFPT_MULTIPART_REQUEST, xid=xid, body=body)


def pack_group_stats_request(group_id: int = OFPG_ALL, xid: int = 0) -> Message:
    return _pack_id_stats_request(OFPMP_GROUP, group_id, xid)


def pack_group_desc_request(xid: int = 0) -> Message:
    body = MULTIPART_HEADER.pack(OFPMP_GROUP_DESC, 0)
    return Message(OFPT_MULTIPART_REQUEST, xid=xid, body=body)


def parse_group_desc_request(body: bytes) -> None:
    if body:
        raise ValueError("yêu cầu GROUP_DESC không có phần thân")


def pack_meter_stats_request(meter_id: int = OFPM_ALL, xid: int = 0) -> Message:
    return _pack_id_stats_request(OFPMP_METER, meter_id, xid)


def pack_meter_config_request(meter_id: int = OFPM_ALL, xid: int = 0) -> Message:
    return _pack_id_stats_request(OFPMP_METER_CONFIG, meter_id, xid)


def pack_port_stats_request(port_no: int = OFPP_ANY, xid: int = 0) -> Message:
    body = MULTIPART_HEADER.pack(OFPMP_PORT_STATS, 0) + PORT_STATS_REQUEST.pack(port_no)
    return Message(OFPT_MULTIPART_REQUEST, xid=xid, body=body)


def parse_port_stats_request(body: bytes) -> int:
    """Phần thân của yêu cầu PORT_STATS -> số cổng (OFPP_ANY = mọi cổng)."""
    if len(body) < PORT_STATS_REQUEST.size:
        raise ValueError("yêu cầu PORT_STATS bị cắt ngắn")
    return PORT_STATS_REQUEST.unpack_from(body)[0]


def pack_port_stats_reply(stats: list[PortStats], xid: int = 0) -> list[Message]:
    return _multipart_replies(OFPMP_PORT_STATS, [PORT_STATS_ENTRY.pack(*s) for s in stats], xid)


def parse_port_stats_reply(msg: Message) -> list[PortStats]:
    """Một thông điệp trả lời PORT_STATS -> danh sách PortStats."""
    mp_type, _flags, body = parse_multipart_reply(msg)
    if mp_type != OFPMP_PORT_STATS:
        raise ValueError(f"không phải trả lời PORT_STATS (multipart type={mp_type})")
    if len(body) % PORT_STATS_ENTRY.size:
        raise ValueError("độ dài PORT_STATS không chia hết cho kích thước một mục")
    return [PortStats(*PORT_STATS_ENTRY.unpack_from(body, off))
            for off in range(0, len(body), PORT_STATS_ENTRY.size)]


def _pack_flow_stats_request(mp_type: int, request: FlowStatsRequest | None, xid: int) -> Message:
    request = request or FlowStatsRequest(OFPTT_ALL, None, {})
    out_port = OFPP_ANY if request.out_port is None else _flow_port_to_wire(request.out_port)
    out_group = OFPG_ANY if request.out_group is None else request.out_group
    body = MULTIPART_HEADER.pack(mp_type, 0)
    body += FLOW_STATS_REQUEST_FIXED.pack(
        request.table_id, out_port, out_group, request.cookie, request.cookie_mask)
    body += _pack_match(_match_to_oxm(request.match))
    return Message(OFPT_MULTIPART_REQUEST, xid=xid, body=body)


def pack_flow_stats_request(request: FlowStatsRequest | None = None, xid: int = 0) -> Message:
    return _pack_flow_stats_request(OFPMP_FLOW, request, xid)


def pack_aggregate_stats_request(request: FlowStatsRequest | None = None,
                                 xid: int = 0) -> Message:
    return _pack_flow_stats_request(OFPMP_AGGREGATE, request, xid)


def parse_flow_stats_request(body: bytes) -> FlowStatsRequest:
    """Phần thân của yêu cầu FLOW_STATS. Trường match lạ bị từ chối (ValueError)."""
    if len(body) < FLOW_STATS_REQUEST_FIXED.size:
        raise ValueError("yêu cầu FLOW_STATS bị cắt ngắn")
    table_id, out_port, out_group, cookie, cookie_mask = FLOW_STATS_REQUEST_FIXED.unpack_from(body)
    fields, _end = _split_match(body, FLOW_STATS_REQUEST_FIXED.size)
    return FlowStatsRequest(
        table_id,
        None if out_port == OFPP_ANY else _flow_port_from_wire(out_port),
        _parse_match_fields(fields),
        cookie,
        cookie_mask,
        None if out_group == OFPG_ANY else out_group,
    )


def pack_flow_stats_reply(flows: list[FlowStats], xid: int = 0) -> list[Message]:
    entries = []
    for flow in flows:
        if (flow.group_id is None) == (flow.out_port is None):
            raise ValueError("flow stats cần đúng một trong out_port hoặc group_id")
        if flow.group_id is None:
            action = _output_action(_flow_port_to_wire(flow.out_port))
        else:
            action = ACTION_GROUP.pack(OFPAT_GROUP, ACTION_GROUP.size, flow.group_id)
        instructions = b""
        if flow.meter_id is not None:
            instructions += INSTRUCTION_METER.pack(
                OFPIT_METER, INSTRUCTION_METER.size, flow.meter_id)
        instructions += INSTRUCTION_HEADER.pack(
            OFPIT_APPLY_ACTIONS, INSTRUCTION_HEADER.size + len(action)) + action
        match = _pack_match(_match_to_oxm(flow.match))
        length = FLOW_STATS_ENTRY_FIXED.size + len(match) + len(instructions)
        entries.append(FLOW_STATS_ENTRY_FIXED.pack(
            length, 0, flow.duration_sec, flow.duration_nsec, flow.priority,
            _timeout_to_wire(flow.idle_timeout), 0, 0, flow.cookie,
            flow.packet_count, flow.byte_count,
        ) + match + instructions)
    return _multipart_replies(OFPMP_FLOW, entries, xid)


def parse_flow_stats_reply(msg: Message) -> list[FlowStats]:
    """Một thông điệp trả lời FLOW_STATS -> danh sách FlowStats."""
    mp_type, _flags, body = parse_multipart_reply(msg)
    if mp_type != OFPMP_FLOW:
        raise ValueError(f"không phải trả lời FLOW_STATS (multipart type={mp_type})")
    flows = []
    pos = 0
    while pos < len(body):
        if len(body) - pos < FLOW_STATS_ENTRY_FIXED.size:
            raise ValueError("mục FLOW_STATS bị cắt ngắn")
        (length, _table, sec, nsec, priority, idle, _hard, _flags2,
         cookie, packets, nbytes) = FLOW_STATS_ENTRY_FIXED.unpack_from(body, pos)
        if length < FLOW_STATS_ENTRY_FIXED.size or pos + length > len(body):
            raise ValueError("độ dài mục FLOW_STATS không hợp lệ")
        entry = body[pos:pos + length]
        fields, end = _split_match(entry, FLOW_STATS_ENTRY_FIXED.size)
        match = _parse_match_fields(fields)
        out_port = group_id = meter_id = None
        action_count = meter_count = 0
        ipos = end
        while ipos < len(entry):
            if len(entry) - ipos < INSTRUCTION_HEADER.size:
                raise ValueError("instruction bị cắt ngắn")
            itype, ilen = INSTRUCTION_HEADER.unpack_from(entry, ipos)
            if ilen < INSTRUCTION_HEADER.size or ilen % 8 or ipos + ilen > len(entry):
                raise ValueError("độ dài instruction không hợp lệ")
            if itype == OFPIT_METER:
                if ilen != INSTRUCTION_METER.size or meter_count:
                    raise ValueError("instruction METER không hợp lệ hoặc lặp")
                meter_id = INSTRUCTION_METER.unpack_from(entry, ipos)[2]
                meter_count += 1
            elif itype == OFPIT_APPLY_ACTIONS:
                if action_count:
                    raise ValueError("instruction APPLY_ACTIONS bị lặp")
                out_port, group_id = _flow_action(
                    entry[ipos + INSTRUCTION_HEADER.size:ipos + ilen])
                action_count += 1
            else:
                raise ValueError(f"instruction type {itype} chưa được hỗ trợ")
            ipos += ilen
        if action_count != 1:
            raise ValueError("flow stats cần đúng một instruction APPLY_ACTIONS")
        flows.append(FlowStats(
            match, out_port, priority, float(idle) if idle else None, packets, nbytes,
            sec, nsec, cookie, group_id, meter_id))
        pos += length
    return flows