class LearningController:
    def __init__(self):
        self.table = {}   # {tên switch: {mac: port}}

    def on_packet_in(self, sw, in_port, pkt):
        t = self.table.setdefault(sw.name, {})
        t[pkt.eth_src] = in_port
        out = t.get(pkt.eth_dst, "FLOOD")
        if out != "FLOOD":
            sw.flow_mod({"eth_dst": pkt.eth_dst}, out)
        sw.packet_out(pkt, out, in_port)