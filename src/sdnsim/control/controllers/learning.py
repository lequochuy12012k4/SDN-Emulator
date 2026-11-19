from sdnsim.core.messages import FlowMod, PacketOut


class LearningController:
    def __init__(self, idle_timeout=None):
        self.table = {}   # {tên switch: {mac: port}}
        self.idle_timeout = idle_timeout
        self.channel = None

    def on_packet_in(self, msg):
        t = self.table.setdefault(msg.switch, {})
        t[msg.pkt.eth_src] = msg.in_port
        out = t.get(msg.pkt.eth_dst, "FLOOD")
        if out != "FLOOD":
            self.channel.send(FlowMod(
                msg.switch, {"eth_dst": msg.pkt.eth_dst}, out,
                idle_timeout=self.idle_timeout))
        self.channel.send(PacketOut(msg.switch, msg.pkt, out, msg.in_port))