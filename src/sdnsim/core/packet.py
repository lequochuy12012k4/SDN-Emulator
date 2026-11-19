from dataclasses import dataclass

# Kích thước khung Ethernet thật (byte), phải khớp với openflow/frame.py
ARP_FRAME_BYTES = 14 + 28              # Ethernet + ARP
ICMP_FRAME_BYTES = 14 + 20 + 8 + 8     # Ethernet + IPv4 + ICMP + dữ liệu (sent_at)
DEFAULT_FRAME_BYTES = 64               # kind lạ: giữ giá trị giả lập cũ


@dataclass
class Packet:
    eth_src: str
    eth_dst: str
    kind: str          # "icmp_req" | "icmp_rep" | "arp_req" | "arp_rep"
    seq: int = 0
    sent_at: float = 0.0
    ip_src: str | None = None
    ip_dst: str | None = None

    @property
    def size(self) -> int:
        if self.kind.startswith("arp"):
            return ARP_FRAME_BYTES
        if self.kind.startswith("icmp"):
            return ICMP_FRAME_BYTES
        return DEFAULT_FRAME_BYTES