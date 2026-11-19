"""Tests for the network builder."""

import pytest

from sdnsim.topology.network import load_network_from_json

from pathlib import Path

TOPOLOGY = Path(__file__).resolve().parent.parent / "topologies" / "two_switch.json"

def test_load_network_from_json():
    """Test loading a network from the two_switch.json topology file."""
    sched, ch, nodes = load_network_from_json(TOPOLOGY)

    # Verify we have all 5 nodes (3 hosts + 2 switches)
    assert len(nodes) == 5
    assert set(nodes.keys()) == {"h1", "h2", "h3", "s1", "s2"}

    # Verify node types
    from sdnsim.core.host import Host
    from sdnsim.core.switch import Switch

    assert isinstance(nodes["h1"], Host)
    assert isinstance(nodes["h2"], Host)
    assert isinstance(nodes["h3"], Host)
    assert isinstance(nodes["s1"], Switch)
    assert isinstance(nodes["s2"], Switch)

    # Verify host properties
    assert nodes["h1"].mac == "00:00:00:00:00:01"
    assert nodes["h1"].ip == "10.0.0.1"
    assert nodes["h2"].mac == "00:00:00:00:00:02"
    assert nodes["h2"].ip == "10.0.0.2"
    assert nodes["h3"].mac == "00:00:00:00:00:03"
    assert nodes["h3"].ip == "10.0.0.3"

    # Verify links were created (each node should have ports)
    assert len(nodes["h1"].ports) == 1
    assert len(nodes["h2"].ports) == 1
    assert len(nodes["h3"].ports) == 1
    assert len(nodes["s1"].ports) == 3  # h1, h2, s2
    assert len(nodes["s2"].ports) == 2  # s1, h3

def test_loaded_network_pings_with_expected_rtt():
    sched, ch, nodes = load_network_from_json(TOPOLOGY)
    h1, h3 = nodes["h1"], nodes["h3"]

    for seq in (1, 2, 3):
        sched.schedule(0.1, h1.ping, h3.mac, seq, h3.ip)
        sched.run()

    assert h1.rtts == [pytest.approx(0.010), pytest.approx(0.008), pytest.approx(0.006)]
    assert ch.packet_in_by_switch == {"s1": 3, "s2": 3}