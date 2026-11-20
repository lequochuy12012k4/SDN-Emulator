import pytest
from pathlib import Path

from sdnsim.core.messages import PacketIn
from sdnsim.topology.network import build_network, load_network_from_json

TOPOLOGY = Path(__file__).resolve().parent / "fixtures" / "two_switch_three_host.json"


def test_controller_receives_packet_in_messages():
    sched, ch, nodes = load_network_from_json(TOPOLOGY)
    seen, orig = [], ch.controller.on_packet_in
    ch.controller.on_packet_in = lambda msg: (seen.append(msg), orig(msg))

    h1, h3 = nodes["h1"], nodes["h3"]
    sched.schedule(0, h1.ping, h3.mac, 1, h3.ip)
    sched.run()

    assert all(isinstance(m, PacketIn) for m in seen)
    assert (seen[0].switch, seen[0].in_port) == ("s1", 1)
    assert len(seen) == 4


def test_downlink_latency_adds_one_hop_per_controller_trip():
    import json
    spec = {**json.loads(TOPOLOGY.read_text(encoding="utf-8")),
            "channel_down_latency": 0.001}
    sched, ch, nodes = build_network(spec)
    h1, h3 = nodes["h1"], nodes["h3"]
    for seq in (1, 2, 3):
        sched.schedule(0.1, h1.ping, h3.mac, seq, h3.ip)
        sched.run()

    # ping 1: 4 lần đi vòng qua controller (+4 ms), ping 2: 2 lần (+2 ms), ping 3: 0
    assert h1.rtts == [pytest.approx(0.014), pytest.approx(0.010), pytest.approx(0.006)]