from __future__ import annotations

import socket
import struct
import threading
from typing import Any

from sdnsim.openflow.frame import packet_to_bytes
from sdnsim.openflow.header import (
    Message,
    OFPT_FLOW_MOD,
    OFPT_PACKET_IN,
    OFPT_PACKET_OUT,
    pack_message,
)

PCAP_GLOBAL_HEADER = struct.Struct("<IHHiIII").pack(
    0xA1B2C3D4,  # magic number (microsecond, little-endian)
    2, 4,        # libpcap version 2.4
    0, 0,        # timezone, sigfigs
    65535,       # snaplen
    1,           # LINKTYPE_ETHERNET
)
PCAP_PKT_HEADER = struct.Struct("<IIII")
TZSP_ETH_HEADER = b"\x01\x00\x00\x01\x01"

OFP_NO_BUFFER = 0xFFFFFFFF
OFPP_CONTROLLER = 0xFFFFFFFD
OFPP_FLOOD = 0xFFFFFFFB
OFPP_ANY = 0xFFFFFFFF


def _checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def _mac_bytes(mac: str) -> bytes:
    return bytes(int(p, 16) for p in mac.split(":"))


def _oxm_match(match: dict[str, Any]) -> bytes:
    tlvs = bytearray()
    if "in_port" in match:
        tlvs += struct.pack("!II", 0x80000004, int(match["in_port"]))
    if "eth_dst" in match:
        tlvs += struct.pack("!I", 0x80000606) + _mac_bytes(match["eth_dst"])
    if "eth_src" in match:
        tlvs += struct.pack("!I", 0x80000806) + _mac_bytes(match["eth_src"])
    length = 4 + len(tlvs)
    raw = struct.pack("!HH", 1, length) + bytes(tlvs)
    pad = (8 - (len(raw) % 8)) % 8
    return raw + (b"\x00" * pad)


def _ofp_port(port: Any) -> int:
    if port == "FLOOD":
        return OFPP_FLOOD
    if port == "CONTROLLER":
        return OFPP_CONTROLLER
    return int(port)


def encode_packet_in(in_port: int, pkt: Any, xid: int) -> bytes:
    eth = packet_to_bytes(pkt)
    fixed = struct.pack("!IHBBQ", OFP_NO_BUFFER, len(eth), 0, 0, 0)
    match = _oxm_match({"in_port": in_port})
    return pack_message(Message(OFPT_PACKET_IN, xid=xid, body=fixed + match + b"\x00\x00" + eth))


def encode_packet_out(pkt: Any, out_port: Any, in_port: int | None, xid: int) -> bytes:
    eth = packet_to_bytes(pkt)
    of_in = OFPP_CONTROLLER if in_port is None else int(in_port)
    action = struct.pack("!HHIH6x", 0, 16, _ofp_port(out_port), 0xFFFF)
    fixed = struct.pack("!IIH6x", OFP_NO_BUFFER, of_in, len(action))
    return pack_message(Message(OFPT_PACKET_OUT, xid=xid, body=fixed + action + eth))


def encode_flow_mod(
    match: dict[str, Any], out_port: Any, priority: int, idle_timeout: float | None, xid: int
) -> bytes:
    idle = int(idle_timeout) if idle_timeout else 0
    fixed = struct.pack(
        "!QQBBHHHIIIH2x",
        0, 0, 0, 0, idle, 0, int(priority),
        OFP_NO_BUFFER, OFPP_ANY, OFPP_ANY, 0,
    )
    m_bytes = _oxm_match(match)
    action = struct.pack("!HHIH6x", 0, 16, _ofp_port(out_port), 0xFFFF)
    inst = struct.pack("!HH4x", 4, 8 + len(action)) + action
    return pack_message(Message(OFPT_FLOW_MOD, xid=xid, body=fixed + m_bytes + inst))


class PacketCapture:
    """Bắt gói tin Data Plane (Link) và Control Plane (OpenFlow 1.3) cho Wireshark."""

    def __init__(self, live_tzsp: bool = False, tzsp_port: int = 37008, maxlen: int = 10000) -> None:
        self.live_tzsp = live_tzsp
        self.tzsp_port = tzsp_port
        self.maxlen = maxlen
        self._records: list[tuple[float, bytes]] = []
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._sink_sock: socket.socket | None = None
        self._xid = 0
        self._sw_ids: dict[str, int] = {}
        self._tcp_streams: dict[str, dict[str, int]] = {}
        if live_tzsp:
            self.set_live(True)

    def _stop_sink(self) -> None:
        if self._sink_sock is not None:
            try:
                self._sink_sock.close()
            except OSError:
                pass
            self._sink_sock = None

    def set_live(self, enabled: bool) -> bool:
        with self._lock:
            self.live_tzsp = bool(enabled)
            if self.live_tzsp:
                if self._sock is None:
                    self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self._start_sink()
            else:
                self._stop_sink()
            return self.live_tzsp
    
    def set_live(self, enabled: bool) -> bool:
        with self._lock:
            self.live_tzsp = bool(enabled)
            if self.live_tzsp and self._sock is None:
                self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            return self.live_tzsp

    def count(self) -> int:
        with self._lock:
            return len(self._records)

    def clear(self) -> None:
        with self._lock:
            self._records.clear()
            self._tcp_streams.clear()

    def _emit_frame(self, sim_time: float, raw: bytes) -> None:
        with self._lock:
            if len(self._records) >= self.maxlen:
                self._records.pop(0)
            self._records.append((float(sim_time), raw))
            live = self.live_tzsp
            sock = self._sock

        if live and sock is not None:
            try:
                sock.sendto(TZSP_ETH_HEADER + raw, ("127.0.0.1", self.tzsp_port))
            except OSError:
                pass

    def record(self, sim_time: float, pkt: Any) -> None:
        try:
            raw = packet_to_bytes(pkt)
        except Exception:
            return
        self._emit_frame(sim_time, raw)

    def _wrap_tcp_openflow(self, sw_name: str, from_switch: bool, payload: bytes) -> bytes:
        with self._lock:
            sw_idx = self._sw_ids.setdefault(sw_name, len(self._sw_ids) + 1)
            st = self._tcp_streams.setdefault(sw_name, {"sw_seq": 1, "ctrl_seq": 1})
            if from_switch:
                seq, ack = st["sw_seq"], st["ctrl_seq"]
                st["sw_seq"] += len(payload)
            else:
                seq, ack = st["ctrl_seq"], st["sw_seq"]
                st["ctrl_seq"] += len(payload)

        sw_mac = bytes([0x00, 0x00, 0x00, 0x00, 0xFE, sw_idx & 0xFF])
        ctrl_mac = b"\x00\x00\x00\x00\xFF\x00"
        sw_ip = socket.inet_aton(f"10.255.0.{10 + (sw_idx % 240)}")
        ctrl_ip = socket.inet_aton("10.255.0.1")
        sw_port = 50000 + sw_idx
        ctrl_port = 6653

        src_mac, dst_mac = (sw_mac, ctrl_mac) if from_switch else (ctrl_mac, sw_mac)
        src_ip, dst_ip = (sw_ip, ctrl_ip) if from_switch else (ctrl_ip, sw_ip)
        src_p, dst_p = (sw_port, ctrl_port) if from_switch else (ctrl_port, sw_port)

        eth = dst_mac + src_mac + struct.pack("!H", 0x0800)

        tcp_len = 20 + len(payload)
        tcp_no_csum = struct.pack("!HHIIBBHHH", src_p, dst_p, seq, ack, 0x50, 0x18, 65535, 0, 0)
        pseudo = struct.pack("!4s4sBBH", src_ip, dst_ip, 0, 6, tcp_len)
        tcp_csum = _checksum(pseudo + tcp_no_csum + payload)
        tcp = struct.pack("!HHIIBBHHH", src_p, dst_p, seq, ack, 0x50, 0x18, 65535, tcp_csum, 0)

        ip_total = 20 + tcp_len
        ip_no_csum = struct.pack("!BBHHHBBH4s4s", 0x45, 0, ip_total, 1, 0x4000, 64, 6, 0, src_ip, dst_ip)
        ip_csum = _checksum(ip_no_csum)
        ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, ip_total, 1, 0x4000, 64, 6, ip_csum, src_ip, dst_ip)

        return eth + ip + tcp + payload

    def record_packet_in(self, sim_time: float, sw_name: str, in_port: int, pkt: Any) -> None:
        try:
            with self._lock:
                self._xid += 1
                xid = self._xid
            of_bytes = encode_packet_in(in_port, pkt, xid)
            frame = self._wrap_tcp_openflow(sw_name, from_switch=True, payload=of_bytes)
            self._emit_frame(sim_time, frame)
        except Exception:
            pass

    def record_packet_out(
        self, sim_time: float, sw_name: str, pkt: Any, out_port: Any, in_port: int | None
    ) -> None:
        try:
            with self._lock:
                self._xid += 1
                xid = self._xid
            of_bytes = encode_packet_out(pkt, out_port, in_port, xid)
            frame = self._wrap_tcp_openflow(sw_name, from_switch=False, payload=of_bytes)
            self._emit_frame(sim_time, frame)
        except Exception:
            pass

    def record_flow_mod(
        self, sim_time: float, sw_name: str, match: dict[str, Any], out_port: Any,
        priority: int = 1, idle_timeout: float | None = None,
    ) -> None:
        try:
            with self._lock:
                self._xid += 1
                xid = self._xid
            of_bytes = encode_flow_mod(match, out_port, priority, idle_timeout, xid)
            frame = self._wrap_tcp_openflow(sw_name, from_switch=False, payload=of_bytes)
            self._emit_frame(sim_time, frame)
        except Exception:
            pass

    def to_pcap_bytes(self) -> bytes:
        with self._lock:
            snapshot = list(self._records)
        chunks = [PCAP_GLOBAL_HEADER]
        for ts, raw in snapshot:
            sec = int(ts)
            usec = int((ts - sec) * 1_000_000)
            chunks.append(PCAP_PKT_HEADER.pack(sec, usec, len(raw), len(raw)))
            chunks.append(raw)
        return b"".join(chunks)

    def close(self) -> None:
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None