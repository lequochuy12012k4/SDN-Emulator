from dataclasses import dataclass


@dataclass
class Packet:
    eth_src: str
    eth_dst: str
    kind: str          # "icmp_req" | "icmp_rep"
    seq: int = 0
    sent_at: float = 0.0