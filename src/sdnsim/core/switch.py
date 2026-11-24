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

def _parse_bandwidth(bw) -> float | None:
    if bw is None or bw == "" or bw == 0:
        return None
    if isinstance(bw, (int, float)):
        val = float(bw)
        if val <= 0:
            raise ValueError(f"Băng thông switch phải > 0, nhận {bw!r}")
        return val
    s = str(bw).strip().lower().replace(" ", "")
    units = (
        ("gbps", 1e9), ("gb/s", 1e9),
        ("mbps", 1e6), ("mb/s", 1e6),
        ("kbps", 1e3), ("kb/s", 1e3),
        ("bps", 1.0), ("b/s", 1.0),
    )
    for suffix, mult in units:
        if s.endswith(suffix):
            val = float(s[:-len(suffix)]) * mult
            if val <= 0:
                raise ValueError(f"Băng thông switch phải > 0, nhận {bw!r}")
            return val
    val = float(s)
    if val <= 0:
        raise ValueError(f"Băng thông switch phải > 0, nhận {bw!r}")
    return val

class Switch:
    def __init__(self, sched, name, channel, delay=0.0, bandwidth=None, queue_limit=None):
        self.sched, self.name, self.channel = sched, name, channel
        self.delay = float(delay or 0.0)
        if self.delay < 0:
            raise ValueError(f"Độ trễ switch phải >= 0, nhận {self.delay}")
        self.bandwidth = _parse_bandwidth(bandwidth)
        self.queue_limit = int(queue_limit) if queue_limit not in (None, "", 0) else None
        self._busy_until = 0.0
        self._queued = 0
        self.dropped_packets = 0

        self.ports = {}
        self.flows = []
        self.groups = {}
        self.meters = {}
        self._sweep_scheduled = False
        self.port_stats = defaultdict(_new_port_stats)
        self.table_misses = 0
        self.table_lookups = 0
        self.table_matched = 0
        channel.register(self)

    def receive(self, in_port, pkt):
        s = self.port_stats[in_port]
        s["rx_packets"] += 1
        s["rx_bytes"] += pkt.size

        if self.delay == 0.0 and self.bandwidth is None:
            self._process_packet(in_port, pkt)
            return

        if self.queue_limit is not None and self._queued >= self.queue_limit:
            self.dropped_packets += 1
            return

        now = self.sched.now
        tx_time = (pkt.size * 8) / self.bandwidth if self.bandwidth else 0.0
        start = max(now, self._busy_until)
        finish = start + tx_time + self.delay
        self._busy_until = start + tx_time
        self._queued += 1
        self.sched.schedule(finish - now, self._finish_packet, in_port, pkt)

    def _finish_packet(self, in_port, pkt):
        self._queued -= 1
        self._process_packet(in_port, pkt)

    def _process_packet(self, in_port, pkt):
        now = self.sched.now
        self.table_lookups += 1
        self.flows = [
            f for f in self.flows
            if f["idle_timeout"] is None or (now - f["last_hit"]) < f["idle_timeout"]
        ]
        for f in sorted(self.flows, key=lambda f: -f["priority"]):
            if flow_matches(f["match"], in_port, pkt):
                self.table_matched += 1
                f["packets"] += 1
                f["bytes"] += pkt.size
                f["last_hit"] = now

                # 1. Áp dụng Meter (nếu có)
                meter_id = f.get("meter_id")
                if meter_id is not None and not self._meter_allows(meter_id, pkt):
                    return

                # 2. Áp dụng Group hoặc chuyển tiếp trực tiếp ra cổng
                group_id = f.get("group_id")
                if group_id is not None:
                    self._group_output(group_id, pkt)
                elif f.get("out_port") is not None:
                    if f["out_port"] == "FLOOD":
                        self.packet_out(pkt, "FLOOD", in_port=in_port)
                    else:
                        self._tx(f["out_port"], pkt)
                return

        self.table_misses += 1
        print(f"[{now*1000:7.2f} ms] {self.name}: table-miss -> PACKET_IN ({pkt.eth_src}->{pkt.eth_dst})")
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
        self.flows.append({
            "match": match, "out_port": out_port, "priority": priority,
            "packets": 0, "bytes": 0, "idle_timeout": idle_timeout,
            "cookie": cookie, "group_id": group_id, "meter_id": meter_id,
            "installed_at": self.sched.now,
            "last_hit": self.sched.now
        })
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
        # OpenFlow burst_size tính theo kilobits; nếu không có cờ BURST, cho phép 1 giây tốc độ.
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
            self._ensure_sweep()

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