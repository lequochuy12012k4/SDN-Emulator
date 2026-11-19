import json
from pathlib import Path

from sdnsim.core.network import load_network_from_json
from sdnsim.metrics.collector import Metrics

TOPOLOGY = Path(__file__).resolve().parent.parent / "topologies" / "two_switch.json"


def run_pings(n):
    sched, ch, nodes = load_network_from_json(TOPOLOGY)
    h1, h3 = nodes["h1"], nodes["h3"]
    for seq in range(1, n + 1):
        sched.schedule(0.1, h1.ping, h3.mac, seq, h3.ip)
        sched.run()
    return Metrics(sched, ch, nodes).snapshot(), ch


def test_port_counters_after_one_ping():
    snap, _ = run_pings(1)
    s1 = snap["switches"]["s1"]["ports"]
    # request vào cổng 1, flood ra cổng 2 và 3; reply vào cổng 3, ra cổng 1
    assert (s1[1]["rx_packets"], s1[1]["tx_packets"]) == (1, 1)
    assert (s1[2]["rx_packets"], s1[2]["tx_packets"]) == (0, 1)
    assert (s1[3]["rx_packets"], s1[3]["tx_packets"]) == (1, 1)
    assert s1[1]["rx_bytes"] == 64


def test_flow_counters_after_three_pings():
    snap, _ = run_pings(3)
    flows = {f["match"]["eth_dst"]: f for f in snap["switches"]["s1"]["flows"]}
    assert flows["00:00:00:00:00:01"]["packets"] == 2   # reply ở ping 2 và 3
    assert flows["00:00:00:00:00:03"]["packets"] == 1   # request ở ping 3
    assert flows["00:00:00:00:00:01"]["bytes"] == 128


def test_table_misses_equal_packet_in():
    snap, ch = run_pings(3)
    for name, sw in snap["switches"].items():
        assert sw["table_misses"] == ch.packet_in_by_switch[name]


def test_snapshot_is_json_serializable():
    snap, _ = run_pings(1)
    assert json.loads(json.dumps(snap))["packet_in_total"] == snap["packet_in_total"]

def test_snapshot_lists_each_link_once():
    snap, _ = run_pings(1)
    links = [frozenset({(l["a"], l["a_port"]), (l["b"], l["b_port"])})
             for l in snap["links"]]
    assert len(links) == 4          # mỗi link đúng một lần, không lặp hai chiều
    assert set(links) == {
        frozenset({("h1", 1), ("s1", 1)}),
        frozenset({("h2", 1), ("s1", 2)}),
        frozenset({("s1", 3), ("s2", 1)}),
        frozenset({("h3", 1), ("s2", 2)}),
    }