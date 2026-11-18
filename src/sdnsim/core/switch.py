from collections import defaultdict

SWEEP_INTERVAL = 0.1 

def _new_port_stats():
    return {"rx_packets": 0, "rx_bytes": 0, "tx_packets": 0, "tx_bytes": 0}

class Switch:
    def __init__(self, sched, name, channel):
        self.sched, self.name, self.channel = sched, name, channel
        self.ports = {}
        self.flows = []
        self._sweep_scheduled = False
        self.port_stats = defaultdict(_new_port_stats)
        self.table_misses = 0

    def receive(self, in_port, pkt):
        # Lazy expiration: remove flows that have exceeded idle_timeout
        s = self.port_stats[in_port]
        s["rx_packets"] += 1
        s["rx_bytes"] += pkt.size
        now = self.sched.now
        self.flows = [
            f for f in self.flows
            if f["idle_timeout"] is None or (now - f["last_hit"]) < f["idle_timeout"]
            
        ]

        for f in sorted(self.flows, key=lambda f: -f["priority"]):
            if all(getattr(pkt, k) == v for k, v in f["match"].items()):
                f["packets"] += 1
                f["bytes"] += pkt.size
                f["last_hit"] = self.sched.now
                self._tx(f["out_port"], pkt)
                return
        self.table_misses += 1
        print(f"[{self.sched.now*1000:7.2f} ms] {self.name}: table-miss -> PACKET_IN ({pkt.eth_src}->{pkt.eth_dst})")
        self.channel.packet_in(self, in_port, pkt)

    def flow_mod(self, match, out_port, priority=1, idle_timeout=None):
        print(f"[{self.sched.now*1000:7.2f} ms] {self.name}: FLOW_MOD match={match} -> port {out_port}")
        self.flows.append({"match": match, "out_port": out_port, "priority": priority,
                            "packets": 0, "bytes": 0, "idle_timeout": idle_timeout,
                        "last_hit": self.sched.now})
        if idle_timeout is not None:
            self._ensure_sweep()

    def _ensure_sweep(self):
        if not self._sweep_scheduled:
            self._sweep_scheduled = True
            self.sched.schedule(SWEEP_INTERVAL, self._sweep)

    def _sweep(self):
        self._sweep_scheduled = False
        now = self.sched.now
        kept = []
        for f in self.flows:
            t = f["idle_timeout"]
            if t is not None and now - f["last_hit"] >= t:
                print(f"[{now*1000:7.2f} ms] {self.name}: FLOW expired match={f['match']}")
            else:
                kept.append(f)
        self.flows = kept
        if any(f["idle_timeout"] is not None for f in self.flows):
            self._ensure_sweep()    # còn flow có timeout thì quét tiếp, hết thì dừng

    def _tx(self, port, pkt):
        s = self.port_stats[port]
        s["tx_packets"] += 1
        s["tx_bytes"] += pkt.size
        self.ports[port].transmit(self, pkt)

    def packet_out(self, pkt, out_port, in_port=None):
        if out_port == "FLOOD":
            for p, link in self.ports.items():
                if p != in_port:
                    s = self.port_stats[p]
                    s["tx_packets"] += 1
                    s["tx_bytes"] += pkt.size
                    link.transmit(self, pkt)
        else:
            self._tx(out_port, pkt)
