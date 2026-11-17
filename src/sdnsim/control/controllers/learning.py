class LearningController:
    def __init__(self, idle_timeout=None):
        self.table = {}   # {tên switch: {mac: port}}
        self.idle_timeout = idle_timeout

    def on_packet_in(self, sw, in_port, pkt):
        t = self.table.setdefault(sw.name, {})
        t[pkt.eth_src] = in_port
        out = t.get(pkt.eth_dst, "FLOOD")
        if out != "FLOOD":
            sw.flow_mod({"eth_dst": pkt.eth_dst}, out, idle_timeout=self.idle_timeout)
        sw.packet_out(pkt, out, in_port)