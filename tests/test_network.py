import json
import pytest
from pathlib import Path

from sdnsim.core.network import build_network

TOPOLOGY_PATH = Path(__file__).resolve().parent.parent / "topologies" / "two_switch.json"
SPEC = json.loads(TOPOLOGY_PATH.read_text(encoding="utf-8"))


def run_ping(sched, src, dst, seq):
    sched.schedule(0.1, src.ping, dst.mac, seq, dst.ip)
    sched.run()


def test_two_switch_spec_reproduces_old_numbers():
    sched, ch, n = build_network(SPEC)
    h1, h3 = n["h1"], n["h3"]
    for seq in (1, 2, 3):
        run_ping(sched, h1, h3, seq)
    assert h1.rtts == [pytest.approx(0.010), pytest.approx(0.008), pytest.approx(0.006)]
    assert ch.packet_in_by_switch == {"s1": 3, "s2": 3}


def test_channel_latency_is_applied():
    spec = {**SPEC, "channel_latency": 0.005}
    sched, ch, _ = build_network(spec)
    assert ch.latency == 0.005


def test_duplicate_port_is_rejected():
    bad = {**SPEC, "links": SPEC["links"] + [
        {"a": "h2", "a_port": 1, "b": "s2", "b_port": 1}]}   # s2 cổng 1 đã dùng
    with pytest.raises(ValueError):
        build_network(bad)


def test_unknown_node_is_rejected():
    bad = {**SPEC, "links": [{"a": "h9", "a_port": 1, "b": "s1", "b_port": 1}]}
    with pytest.raises(ValueError):
        build_network(bad)