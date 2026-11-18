"""Example: ping between two hosts using the network builder."""

from sdnsim.core.network import load_network_from_json

sched, ch, nodes = load_network_from_json("examples/topology_two_hosts.json")

h1 = nodes["h1"]
h2 = nodes["h2"]

for i, t in enumerate([0.0, 0.1, 0.2], start=1):
    sched.schedule(t, h1.ping, h2.mac, i, h2.ip)

sched.run()