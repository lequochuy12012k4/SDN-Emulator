from sdnsim.core.packet import Packet


class Host:
    def __init__(self, sched, name, mac):
        self.sched, self.name, self.mac = sched, name, mac
        self.ports = {}
        self.rtts = []          # RTT (giây) của các ping đã nhận reply

    def send(self, pkt):
        self.ports[1].transmit(self, pkt)

    def ping(self, dst_mac, seq=1):
        self.send(Packet(self.mac, dst_mac, "icmp_req", seq, self.sched.now))

    def receive(self, port, pkt):
        if pkt.eth_dst != self.mac:
            return
        if pkt.kind == "icmp_req":
            self.send(Packet(self.mac, pkt.eth_src, "icmp_rep", pkt.seq, pkt.sent_at))
        else:
            rtt = self.sched.now - pkt.sent_at
            self.rtts.append(rtt)
            print(f"[{self.sched.now*1000:7.2f} ms] {self.name}: reply seq={pkt.seq} rtt={rtt*1000:.2f} ms")