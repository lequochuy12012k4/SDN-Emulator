class Switch:
    def __init__(self, sched, name, channel):
        self.sched, self.name, self.channel = sched, name, channel
        self.ports = {}
        self.flows = []

    def receive(self, in_port, pkt):
        for f in sorted(self.flows, key=lambda f: -f["priority"]):
            if all(getattr(pkt, k) == v for k, v in f["match"].items()):
                f["packets"] += 1
                self.ports[f["out_port"]].transmit(self, pkt)
                return
        print(f"[{self.sched.now*1000:7.2f} ms] {self.name}: table-miss -> PACKET_IN ({pkt.eth_src}->{pkt.eth_dst})")
        self.channel.packet_in(self, in_port, pkt)

    def flow_mod(self, match, out_port, priority=1):
        print(f"[{self.sched.now*1000:7.2f} ms] {self.name}: FLOW_MOD match={match} -> port {out_port}")
        self.flows.append({"match": match, "out_port": out_port,
                           "priority": priority, "packets": 0})

    def packet_out(self, pkt, out_port, in_port=None):
        if out_port == "FLOOD":
            for p, link in self.ports.items():
                if p != in_port:
                    link.transmit(self, pkt)
        else:
            self.ports[out_port].transmit(self, pkt)