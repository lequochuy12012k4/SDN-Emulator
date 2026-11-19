"""Chuyển Packet của simulator <-> khung Ethernet dạng byte thật (ARP, ICMP echo).

Hạn chế đã biết (do khung thật không có chỗ cho các trường này):
  - Gói ARP không mang `seq` và `sent_at`: giải gói sẽ cho seq=0, sent_at=0.0.
  - IP None được mã hóa thành 0.0.0.0, và 0.0.0.0 giải ra None.
  - Gói ICMP mang `sent_at` trong phần dữ liệu (8 byte), để RTT vẫn tính được.
"""

import struct
from ipaddress import IPv4Address

from sdnsim.core.packet import Packet

ETH = struct.Struct("!6s6sH")             # MAC đích, MAC nguồn, ethertype
ARP = struct.Struct("!HHBBH6s4s6s4s")     # htype, ptype, hlen, plen, op, sha, spa, tha, tpa
IPV4 = struct.Struct("!BBHHHBBH4s4s")     # ver/ihl, tos, tổng dài, id, flags/frag, ttl, proto, csum, src, dst
ICMP = struct.Struct("!BBHHH")            # type, code, csum, id, seq
SENT_AT = struct.Struct("!d")             # 8 byte dữ liệu ICMP: giờ ảo lúc gửi

ETH_ARP = 0x0806
ETH_IPV4 = 0x0800
ARP_REQUEST, ARP_REPLY = 1, 2
ICMP_ECHO_REPLY, ICMP_ECHO_REQUEST = 0, 8
PROTO_ICMP = 1
ZERO_MAC = b"\x00" * 6


def checksum(data: bytes) -> int:
    """Checksum Internet: tổng bù 1 của các từ 16 bit."""
    if len(data) % 2:
        data += b"\x00"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def _mac_to_bytes(mac: str) -> bytes:
    return bytes.fromhex(mac.replace(":", ""))


def _bytes_to_mac(raw: bytes) -> str:
    return ":".join(f"{b:02x}" for b in raw)


def _ip_to_bytes(ip: str | None) -> bytes:
    return IPv4Address(ip or "0.0.0.0").packed


def _bytes_to_ip(raw: bytes) -> str | None:
    ip = str(IPv4Address(raw))
    return None if ip == "0.0.0.0" else ip


def packet_to_bytes(pkt: Packet) -> bytes:
    if pkt.kind in ("arp_req", "arp_rep"):
        return _arp_to_bytes(pkt)
    if pkt.kind in ("icmp_req", "icmp_rep"):
        return _icmp_to_bytes(pkt)
    raise ValueError(f"không biết đóng gói kind={pkt.kind!r}")


def _arp_to_bytes(pkt: Packet) -> bytes:
    is_request = pkt.kind == "arp_req"
    # ARP request chưa biết MAC đích nên trường này là 0; MAC đích Ethernet vẫn là broadcast.
    tha = ZERO_MAC if is_request else _mac_to_bytes(pkt.eth_dst)
    body = ARP.pack(
        1, ETH_IPV4, 6, 4,
        ARP_REQUEST if is_request else ARP_REPLY,
        _mac_to_bytes(pkt.eth_src), _ip_to_bytes(pkt.ip_src),
        tha, _ip_to_bytes(pkt.ip_dst),
    )
    eth = ETH.pack(_mac_to_bytes(pkt.eth_dst), _mac_to_bytes(pkt.eth_src), ETH_ARP)
    return eth + body


def _icmp_to_bytes(pkt: Packet) -> bytes:
    if not 0 <= pkt.seq <= 0xFFFF:
        raise ValueError(f"seq={pkt.seq} không vừa trường 16 bit của ICMP")
    icmp_type = ICMP_ECHO_REQUEST if pkt.kind == "icmp_req" else ICMP_ECHO_REPLY
    icmp = ICMP.pack(icmp_type, 0, 0, 0, pkt.seq) + SENT_AT.pack(pkt.sent_at)
    icmp = icmp[:2] + struct.pack("!H", checksum(icmp)) + icmp[4:]

    ip = IPV4.pack(
        0x45, 0, IPV4.size + len(icmp), 0, 0, 64, PROTO_ICMP, 0,
        _ip_to_bytes(pkt.ip_src), _ip_to_bytes(pkt.ip_dst),
    )
    ip = ip[:10] + struct.pack("!H", checksum(ip)) + ip[12:]

    eth = ETH.pack(_mac_to_bytes(pkt.eth_dst), _mac_to_bytes(pkt.eth_src), ETH_IPV4)
    return eth + ip + icmp


def bytes_to_packet(data: bytes) -> Packet:
    if len(data) < ETH.size:
        raise ValueError("khung ngắn hơn header Ethernet")
    dst, src, ethertype = ETH.unpack_from(data)
    payload = data[ETH.size:]
    if ethertype == ETH_ARP:
        return _arp_from_bytes(src, dst, payload)
    if ethertype == ETH_IPV4:
        return _icmp_from_bytes(src, dst, payload)
    raise ValueError(f"ethertype 0x{ethertype:04x} chưa được hỗ trợ")


def _arp_from_bytes(src: bytes, dst: bytes, payload: bytes) -> Packet:
    if len(payload) < ARP.size:
        raise ValueError("gói ARP bị cắt ngắn")
    htype, ptype, hlen, plen, op, _sha, spa, _tha, tpa = ARP.unpack_from(payload)
    if (htype, ptype, hlen, plen) != (1, ETH_IPV4, 6, 4):
        raise ValueError("chỉ hỗ trợ ARP cho Ethernet/IPv4")
    if op not in (ARP_REQUEST, ARP_REPLY):
        raise ValueError(f"ARP opcode {op} chưa được hỗ trợ")
    return Packet(
        eth_src=_bytes_to_mac(src),
        eth_dst=_bytes_to_mac(dst),
        kind="arp_req" if op == ARP_REQUEST else "arp_rep",
        ip_src=_bytes_to_ip(spa),
        ip_dst=_bytes_to_ip(tpa),
    )


def _icmp_from_bytes(src: bytes, dst: bytes, payload: bytes) -> Packet:
    if len(payload) < IPV4.size:
        raise ValueError("gói IPv4 bị cắt ngắn")
    ver_ihl, _tos, total_len, _id, _frag, _ttl, proto, _csum, ip_src, ip_dst = (
        IPV4.unpack_from(payload)
    )
    ihl = (ver_ihl & 0x0F) * 4
    if ver_ihl >> 4 != 4 or ihl < IPV4.size:
        raise ValueError("header IPv4 không hợp lệ")
    if total_len < ihl + ICMP.size or len(payload) < total_len:
        raise ValueError("độ dài IPv4 không khớp dữ liệu")
    if proto != PROTO_ICMP:
        raise ValueError(f"giao thức IP {proto} chưa được hỗ trợ")
    if checksum(payload[:ihl]) != 0:
        raise ValueError("sai checksum IPv4")

    icmp = payload[ihl:total_len]       # cắt bỏ phần đệm Ethernet nếu có
    if checksum(icmp) != 0:
        raise ValueError("sai checksum ICMP")
    icmp_type, _code, _csum, _ident, seq = ICMP.unpack_from(icmp)
    if icmp_type == ICMP_ECHO_REQUEST:
        kind = "icmp_req"
    elif icmp_type == ICMP_ECHO_REPLY:
        kind = "icmp_rep"
    else:
        raise ValueError(f"ICMP type {icmp_type} chưa được hỗ trợ")

    extra = icmp[ICMP.size:]
    sent_at = SENT_AT.unpack_from(extra)[0] if len(extra) >= SENT_AT.size else 0.0
    return Packet(
        eth_src=_bytes_to_mac(src),
        eth_dst=_bytes_to_mac(dst),
        kind=kind,
        seq=seq,
        sent_at=sent_at,
        ip_src=_bytes_to_ip(ip_src),
        ip_dst=_bytes_to_ip(ip_dst),
    )