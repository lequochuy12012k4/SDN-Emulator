from collections import Counter

from sdnsim.core.messages import PacketIn


class Channel:
    """Kênh switch <-> controller. Giai đoạn 4b sẽ thay bằng socket OpenFlow."""

    def __init__(self, sched, latency=0.001, down_latency=0.0):
        self.sched, self.latency, self.down_latency = sched, latency, down_latency
        self._controller = None
        self.switches = {}
        self.packet_in_count = 0
        self.packet_in_by_switch = Counter()

    @property
    def controller(self):
        return self._controller

    @controller.setter
    def controller(self, c):
        self._controller = c
        if c is not None:
            c.channel = self

    def register(self, sw):
        self.switches[sw.name] = sw

    def packet_in(self, sw, in_port, pkt):
        self.packet_in_count += 1
        self.packet_in_by_switch[sw.name] += 1
        msg = PacketIn(sw.name, in_port, pkt)
        self.sched.schedule(self.latency, self.controller.on_packet_in, msg)

    def send(self, msg):
        sw = self.switches[msg.switch]
        if self.down_latency:
            self.sched.schedule(self.down_latency, sw.handle, msg)
        else:
            sw.handle(msg)     # độ trễ 0: áp dụng ngay, giữ nguyên hành vi cũ