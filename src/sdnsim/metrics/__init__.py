from sdnsim.core.host import Host
from sdnsim.core.switch import Switch


class Metrics:
    """Đọc số liệu từ mạng, không sửa gì. Phụ thuộc core, không ngược lại."""

    def __init__(self, sched, channel, nodes):
        self.sched, self.channel, self.nodes = sched, channel, nodes

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
        }