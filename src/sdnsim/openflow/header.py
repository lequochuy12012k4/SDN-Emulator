import struct
from dataclasses import dataclass

OFP_VERSION = 0x04                      # OpenFlow 1.3
HEADER = struct.Struct("!BBHI")         # version, type, length, xid
HEADER_LEN = HEADER.size                # 8

OFPT_HELLO = 0
OFPT_ERROR = 1
OFPT_ECHO_REQUEST = 2
OFPT_ECHO_REPLY = 3
OFPT_FEATURES_REQUEST = 5
OFPT_FEATURES_REPLY = 6
OFPT_SET_CONFIG = 12
OFPT_PACKET_IN = 10
OFPT_PACKET_OUT = 13
OFPT_FLOW_MOD = 14
OFPT_GROUP_MOD = 15
OFPT_METER_MOD = 29
OFPT_BARRIER_REQUEST = 20
OFPT_BARRIER_REPLY = 21


@dataclass
class Message:
    type: int
    xid: int = 0
    body: bytes = b""
    version: int = OFP_VERSION


def pack_message(msg: Message) -> bytes:
    length = HEADER_LEN + len(msg.body)
    if length > 0xFFFF:
        raise ValueError("thông điệp dài quá 65535 byte")
    return HEADER.pack(msg.version, msg.type, length, msg.xid) + msg.body