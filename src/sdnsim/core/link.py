class Link:
    def __init__(self, sched, a, a_port, b, b_port, delay=0.001):
        self.sched, self.delay = sched, delay
        self.peer = {a: (b, b_port), b: (a, a_port)}
        a.ports[a_port] = self
        b.ports[b_port] = self

    def transmit(self, sender, pkt):
        node, port = self.peer[sender]
        self.sched.schedule(self.delay, node.receive, port, pkt)