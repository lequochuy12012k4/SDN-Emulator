from collections import Counter


class Channel:
    """Kênh switch -> controller. Giai đoạn 4 sẽ thay bằng socket OpenFlow."""

    def __init__(self, sched, latency=0.001):
        self.sched, self.latency, self.controller = sched, latency, None
        self.packet_in_count = 0
        self.packet_in_by_switch = Counter()

    def packet_in(self, sw, in_port, pkt):
        self.packet_in_count += 1
        self.packet_in_by_switch[sw.name] += 1
        self.sched.schedule(self.latency, self.controller.on_packet_in, sw, in_port, pkt)