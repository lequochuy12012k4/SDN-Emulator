from sdnsim.core.packet import Packet

BROADCAST = "ff:ff:ff:ff:ff:ff"
ARP_TIMEOUT = 1.0        # giây chờ ARP reply trước khi hỏi lại
ARP_ATTEMPTS = 3         # tổng số lần gửi ARP request trước khi bỏ cuộc


class Host:
    def __init__(self, sched, name, mac, ip=None):
        self.sched, self.name, self.mac, self.ip = sched, name, mac, ip
        self.arp_table = {}      # {ip: mac}
        self.ports = {}
        self.pending = {}        # {ip đích: [seq các ping đang chờ ARP reply]}
        self.arp_failures = 0    # số ping bị bỏ vì không phân giải được ARP
        self.rtts = []           # RTT (giây) của các ping đã nhận reply

    def send(self, pkt):
        self.ports[1].transmit(self, pkt)

    def ping(self, dst_mac, seq=1, dst_ip=None):
        self.send(Packet(
            self.mac, dst_mac, "icmp_req", seq, self.sched.now,
            ip_src=self.ip, ip_dst=dst_ip,
        ))

    def ping_ip(self, dst_ip, seq=1):
        if dst_ip in self.arp_table:
            self.ping(self.arp_table[dst_ip], seq, dst_ip)
            return
        waiting = self.pending.setdefault(dst_ip, [])
        waiting.append(seq)
        if len(waiting) == 1:                 # ping đầu tiên mới khởi động việc hỏi ARP
            self._ask_arp(dst_ip, 1, seq)

    def _ask_arp(self, dst_ip, attempt, seq):
        if dst_ip not in self.pending:        # đã có ARP reply
            return
        if attempt > ARP_ATTEMPTS:            # hết lượt hỏi: bỏ các ping đang chờ
            self.arp_failures += len(self.pending.pop(dst_ip))
            return
        self.send(Packet(
            self.mac, BROADCAST, "arp_req", seq, self.sched.now,
            ip_src=self.ip, ip_dst=dst_ip,
        ))
        self.sched.schedule(ARP_TIMEOUT, self._ask_arp, dst_ip, attempt + 1, seq)

    def receive(self, port, pkt):
        if pkt.eth_dst not in (self.mac, BROADCAST):
            return
        if pkt.kind == "icmp_req":
            self.send(Packet(
                self.mac, pkt.eth_src, "icmp_rep", pkt.seq, pkt.sent_at,
                ip_src=self.ip, ip_dst=pkt.ip_src,
            ))
        elif pkt.kind == "icmp_rep":
            rtt = self.sched.now - pkt.sent_at
            self.rtts.append(rtt)
            print(f"[{self.sched.now*1000:7.2f} ms] {self.name}: reply seq={pkt.seq} rtt={rtt*1000:.2f} ms")
        elif pkt.kind == "arp_req":
            if pkt.ip_dst == self.ip:
                self.send(Packet(
                    self.mac, pkt.eth_src, "arp_rep", pkt.seq, self.sched.now,
                    ip_src=self.ip, ip_dst=pkt.ip_src,
                ))
        elif pkt.kind == "arp_rep":
            self.arp_table[pkt.ip_src] = pkt.eth_src
            for seq in self.pending.pop(pkt.ip_src, []):
                self.ping(pkt.eth_src, seq, pkt.ip_src)