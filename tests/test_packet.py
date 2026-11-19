import pytest

from sdnsim.core.packet import Packet
from sdnsim.openflow.frame import packet_to_bytes

H1, H3 = "00:00:00:00:00:01", "00:00:00:00:00:03"


@pytest.mark.parametrize("kind", ["arp_req", "arp_rep", "icmp_req", "icmp_rep"])
def test_size_matches_real_frame_length(kind):
    pkt = Packet(H1, H3, kind, seq=1, ip_src="10.0.0.1", ip_dst="10.0.0.3")
    assert pkt.size == len(packet_to_bytes(pkt))


def test_unknown_kind_keeps_default_size():
    assert Packet(H1, H3, "other").size == 64