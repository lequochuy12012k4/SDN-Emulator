from sdnsim.core.host import Host
from sdnsim.core.switch import Switch


class Metrics:
    """Đọc số liệu từ mạng, không sửa gì. Phụ thuộc core, không ngược lại."""

    def __init__(self, sched, channel, nodes):
        self.sched, self.channel, self.nodes = sched, channel, nodes

    def _links(self):
        seen, out = set(), []
        for name, node in self.nodes.items():
            for port, link in node.ports.items():
                peer, peer_port = link.peer[node]
                key = frozenset({(name, port), (peer.name, peer_port)})
                if key in seen:
                    continue
                seen.add(key)
                out.append({"a": name, "a_port": port, "b": peer.name,
                            "b_port": peer_port, "delay": link.delay})
        return out

    def snapshot(self):
        switches, hosts = {}, {}
        for name, node in self.nodes.items():
            if isinstance(node, Switch):
                switches[name] = {
                    "table_misses": node.table_misses,
                    "ports": {p: dict(s) for p, s in node.port_stats.items()},
                    "flows": [{**f, "match": dict(f["match"])} for f in node.flows],
                }
            elif isinstance(node, Host):
                hosts[name] = {"ip": node.ip, "mac": node.mac,
                               "rtts": list(node.rtts)}
        return {
            "time": self.sched.now,
            "packet_in_total": self.channel.packet_in_count,
            "packet_in_by_switch": dict(self.channel.packet_in_by_switch),
            "switches": switches,
            "hosts": hosts,
            "links": self._links(),
            "controller": self._controller_status(),
        }
    
    def _controller_status(self):
        connected = getattr(self.channel, "connected", None)
        if connected is None:
            return {"mode": "internal"}
        return {"mode": "openflow",
                "switches": {n: connected(n) for n in self.channel.switches}}