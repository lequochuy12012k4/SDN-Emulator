import pytest

from sdnsim.core.packet import Packet
from sdnsim.openflow.frame import bytes_to_packet, checksum, packet_to_bytes

H1, H3 = "00:00:00:00:00:01", "00:00:00:00:00:03"
BCAST = "ff:ff:ff:ff:ff:ff"


def arp_req():
    return Packet(H1, BCAST, "arp_req", ip_src="10.0.0.1", ip_dst="10.0.0.3")


def arp_rep():
    return Packet(H3, H1, "arp_rep", ip_src="10.0.0.3", ip_dst="10.0.0.1")


def icmp_req():
    return Packet(H1, H3, "icmp_req", seq=5, sent_at=0.123,
                  ip_src="10.0.0.1", ip_dst="10.0.0.3")


def icmp_rep():
    return Packet(H3, H1, "icmp_rep", seq=5, sent_at=0.123,
                  ip_src="10.0.0.3", ip_dst="10.0.0.1")


@pytest.mark.parametrize("make", [arp_req, arp_rep, icmp_req, icmp_rep])
def test_roundtrip_all_kinds(make):
    pkt = make()
    assert bytes_to_packet(packet_to_bytes(pkt)) == pkt


def test_frame_lengths():
    assert len(packet_to_bytes(arp_req())) == 14 + 28
    assert len(packet_to_bytes(icmp_req())) == 14 + 20 + 8 + 8


def test_arp_request_golden_bytes():
    expected = bytes.fromhex(
        "ffffffffffff" "000000000001" "0806"          # Ethernet
        "0001" "0800" "06" "04" "0001"                # htype, ptype, hlen, plen, op
        "000000000001" "0a000001"                     # MAC/IP người gửi
        "000000000000" "0a000003"                     # MAC đích (chưa biết)/IP đích
    )
    assert packet_to_bytes(arp_req()) == expected


def test_checksum_known_ipv4_header():
    # Ví dụ chuẩn: header IPv4 với trường checksum đặt 0, kết quả phải là 0xb861
    header = bytes.fromhex("450000730000400040110000c0a80001c0a800c7")
    assert checksum(header) == 0xB861


def test_checksum_of_valid_frame_parts_is_zero():
    frame = packet_to_bytes(icmp_req())
    assert checksum(frame[14:34]) == 0      # header IPv4 gồm cả checksum
    assert checksum(frame[34:]) == 0        # ICMP gồm cả checksum


def test_icmp_carries_sent_at_so_rtt_survives():
    req = bytes_to_packet(packet_to_bytes(icmp_req()))
    assert req.sent_at == pytest.approx(0.123)


def test_missing_ip_roundtrips_as_none():
    pkt = Packet(H1, H3, "icmp_req", seq=1)       # ip_src, ip_dst = None
    assert bytes_to_packet(packet_to_bytes(pkt)) == pkt


def test_ethernet_padding_is_ignored():
    frame = packet_to_bytes(icmp_req()) + b"\x00" * 10
    assert bytes_to_packet(frame) == icmp_req()


def test_corrupted_ip_header_is_rejected():
    frame = bytearray(packet_to_bytes(icmp_req()))
    frame[14 + 8] ^= 0xFF                          # sửa TTL, checksum IP không còn đúng
    with pytest.raises(ValueError):
        bytes_to_packet(bytes(frame))


def test_corrupted_icmp_is_rejected():
    frame = bytearray(packet_to_bytes(icmp_req()))
    frame[-1] ^= 0xFF                              # sửa dữ liệu ICMP
    with pytest.raises(ValueError):
        bytes_to_packet(bytes(frame))


def test_truncated_and_unknown_frames_are_rejected():
    with pytest.raises(ValueError):
        bytes_to_packet(b"\x00" * 5)
    with pytest.raises(ValueError):
        bytes_to_packet(packet_to_bytes(icmp_req())[:30])
    with pytest.raises(ValueError):                # ethertype IPv6
        bytes_to_packet(b"\x00" * 12 + b"\x86\xdd" + b"\x00" * 40)
    with pytest.raises(ValueError):
        packet_to_bytes(Packet(H1, H3, "tcp_syn"))


def test_seq_too_large_for_icmp_is_rejected():
    with pytest.raises(ValueError):
        packet_to_bytes(Packet(H1, H3, "icmp_req", seq=70000))