from sdnsim.core.scheduler import Scheduler
from sdnsim.core.host import Host
from sdnsim.core.packet import Packet


class CapturePort:
    def __init__(self):
        self.packets = []

    def transmit(self, sender, pkt):
        self.packets.append(pkt)


def test_ping_and_reply_carry_ip_addresses():
    sched = Scheduler()
    h1 = Host(sched, "h1", "00:00:00:00:00:01", "10.0.0.1")
    port = CapturePort()
    h1.ports[1] = port

    h1.ping("00:00:00:00:00:02", 1, "10.0.0.2")
    request = port.packets[-1]
    assert (request.ip_src, request.ip_dst) == ("10.0.0.1", "10.0.0.2")

    h1.receive(1, Packet(
        "00:00:00:00:00:02", h1.mac, "icmp_req",
        ip_src="10.0.0.2", ip_dst=h1.ip,
    ))
    reply = port.packets[-1]
    assert (reply.ip_src, reply.ip_dst) == ("10.0.0.1", "10.0.0.2")