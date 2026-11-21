from collections import defaultdict
from sdnsim.core.messages import FlowMod, PacketOut

SWEEP_INTERVAL = 0.1 

def _new_port_stats():
    return {
        "rx_packets": 0, "rx_bytes": 0, "tx_packets": 0, "tx_bytes": 0,
        "rx_dropped": 0, "tx_dropped": 0, "rx_errors": 0, "tx_errors": 0,
        "rx_frame_err": 0, "rx_over_err": 0, "rx_crc_err": 0, "collisions": 0,
    }


def flow_matches(match, in_port, pkt):
    """True nếu gói thỏa mọi điều kiện của flow.

    Khóa "in_port" so với cổng mà gói đến; các khóa khác so với thuộc tính cùng tên của Packet.
    """
    for key, want in match.items():
        got = in_port if key == "in_port" else getattr(pkt, key)
        if got != want:
            return False
    return True
class Switch:
    def __init__(self, sched, name, channel):
        self.sched, self.name, self.channel = sched, name, channel
        self.ports = {}
        self.flows = []
        self.groups = {}
        self.meters = {}
        self._sweep_scheduled = False
        self.port_stats = defaultdict(_new_port_stats)
        self.table_lookups = 0
        self.table_matched = 0
        self.table_misses = 0
        channel.register(self)

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

        self.table_lookups += 1
        for f in sorted(self.flows, key=lambda f: -f["priority"]):
            if flow_matches(f["match"], in_port, pkt):
                f["packets"] += 1
                f["bytes"] += pkt.size
                f["last_hit"] = self.sched.now
                self.table_matched += 1
                if f["meter_id"] is not None and not self._meter_allows(f["meter_id"], pkt):
                    return
                if f["group_id"] is not None:
                    self._group_output(f["group_id"], pkt)
                else:
                    self._tx(f["out_port"], pkt)
                return
        self.table_misses += 1
        print(f"[{self.sched.now*1000:7.2f} ms] {self.name}: table-miss -> PACKET_IN ({pkt.eth_src}->{pkt.eth_dst})")
        self.channel.packet_in(self, in_port, pkt)

    def record_rx_error(self, in_port):
        stats = self.port_stats[in_port]
        stats["rx_errors"] += 1
        stats["rx_crc_err"] += 1
        stats["rx_dropped"] += 1

    def flow_mod(self, match, out_port, priority=1, idle_timeout=None, cookie=0,
                 group_id=None, meter_id=None):
        print(f"[{self.sched.now*1000:7.2f} ms] {self.name}: FLOW_MOD match={match} -> port {out_port}")
        self.flows = [
            flow for flow in self.flows
            if not (flow["match"] == match and flow["priority"] == priority)
        ]
        self.flows.append({"match": match, "out_port": out_port, "priority": priority,
                            "packets": 0, "bytes": 0, "idle_timeout": idle_timeout,
                            "cookie": cookie, "group_id": group_id, "meter_id": meter_id,
                        "installed_at": self.sched.now,
                        "last_hit": self.sched.now})
        if idle_timeout is not None:
            self._ensure_sweep()

    def group_mod(self, command, group_type, group_id, buckets):
        if command == 2:
            if group_id == 0xFFFFFFFC:
                self.groups.clear()
            else:
                self.groups.pop(group_id, None)
            return
        self.groups[group_id] = {
            "type": group_type,
            "buckets": [
                {"out_port": bucket.out_port, "weight": bucket.weight,
                 "watch_port": bucket.watch_port, "watch_group": bucket.watch_group,
                 "packets": 0, "bytes": 0}
                for bucket in buckets
            ],
            "packets": 0, "bytes": 0, "installed_at": self.sched.now,
        }

    def meter_mod(self, command, flags, meter_id, rate, burst_size):
        if command == 2:
            if meter_id == 0xFFFFFFFF:
                self.meters.clear()
            else:
                self.meters.pop(meter_id, None)
            return
        rate_bits_per_second = rate * 1000
        # OpenFlow burst_size is in kilobits; without BURST, allow one second of rate.
        capacity_bits = (burst_size * 1000 if flags & 4 else rate_bits_per_second)
        self.meters[meter_id] = {
            "flags": flags, "rate": rate, "burst_size": burst_size,
            "rate_bits_per_second": rate_bits_per_second,
            "capacity_bits": capacity_bits, "tokens_bits": capacity_bits,
            "last_update": self.sched.now, "installed_at": self.sched.now,
            "packets": 0, "bytes": 0, "dropped_packets": 0, "dropped_bytes": 0,
        }

    def _meter_allows(self, meter_id, pkt):
        meter = self.meters.get(meter_id)
        if meter is None:
            return False
        now = self.sched.now
        elapsed = max(0.0, now - meter["last_update"])
        meter["tokens_bits"] = min(
            meter["capacity_bits"],
            meter["tokens_bits"] + elapsed * meter["rate_bits_per_second"])
        meter["last_update"] = now
        meter["packets"] += 1
        meter["bytes"] += pkt.size
        packet_bits = pkt.size * 8
        if meter["tokens_bits"] < packet_bits:
            meter["dropped_packets"] += 1
            meter["dropped_bytes"] += pkt.size
            return False
        meter["tokens_bits"] -= packet_bits
        return True

    def _group_output(self, group_id, pkt):
        group = self.groups.get(group_id)
        if group is None:
            return
        group["packets"] += 1
        group["bytes"] += pkt.size
        for bucket in group["buckets"]:
            bucket["packets"] += 1
            bucket["bytes"] += pkt.size
            self._tx(bucket["out_port"], pkt)

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

    def handle(self, msg):
        if isinstance(msg, FlowMod):
            self.flow_mod(msg.match, msg.out_port, msg.priority, msg.idle_timeout, msg.cookie,
                          msg.group_id, msg.meter_id)
        elif isinstance(msg, PacketOut):
            self.packet_out(msg.pkt, msg.out_port, msg.in_port)