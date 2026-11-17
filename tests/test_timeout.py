import pytest

from sdnsim.core.scheduler import Scheduler
from sdnsim.core.host import Host
from sdnsim.core.switch import Switch
from sdnsim.core.link import Link
from sdnsim.control.channel import Channel
from sdnsim.control.controllers.learning import LearningController


def build(idle_timeout=None):
    sched = Scheduler()
    channel = Channel(sched)
    channel.controller = LearningController(idle_timeout=idle_timeout)
    switch = Switch(sched, "s1", channel)
    h1 = Host(sched, "h1", "00:00:00:00:00:01", "10.0.0.1")
    h2 = Host(sched, "h2", "00:00:00:00:00:02", "10.0.0.2")
    Link(sched, h1, 1, switch, 1)
    Link(sched, h2, 1, switch, 2)
    return sched, channel, h1, h2, switch


def ping_and_run(sched, h1, h2, seq):
    sched.schedule(0.1, h1.ping, h2.mac, seq, h2.ip)
    sched.run()


def test_third_ping_causes_no_packet_in():
    sched, channel, h1, h2, _ = build()

    ping_and_run(sched, h1, h2, 1)
    after_first = channel.packet_in_count
    ping_and_run(sched, h1, h2, 2)
    after_second = channel.packet_in_count
    ping_and_run(sched, h1, h2, 3)
    after_third = channel.packet_in_count

    assert after_first == 2                 # request + reply
    assert after_second == after_first + 1  # chỉ request, reply đã có flow
    assert after_third == after_second      # cả hai chiều đã có flow


def test_rtt_decreases_as_flows_are_installed():
    sched, channel, h1, h2, _ = build()
    for seq in (1, 2, 3):
        ping_and_run(sched, h1, h2, seq)

    assert h1.rtts == [pytest.approx(0.006), pytest.approx(0.005), pytest.approx(0.004)]


def test_idle_timeout_causes_packet_in_after_five_seconds():
    sched, channel, h1, h2, switch = build(idle_timeout=1.0)

    sched.schedule(0.0, h1.ping, h2.mac, 1, h2.ip)
    sched.run(until=0.5)
    assert switch.flows[0]["idle_timeout"] == 1.0
    after_first_ping = channel.packet_in_count

    sched.schedule(5.0 - sched.now, h1.ping, h2.mac, 2, h2.ip)
    sched.run()

    assert channel.packet_in_count == after_first_ping + 2

def test_traffic_refreshes_idle_timeout():
    sched, channel, h1, h2, switch = build(idle_timeout=1.0)
    for t, seq in ((0.0, 1), (0.6, 2)):
        sched.schedule(t, h1.ping, h2.mac, seq, h2.ip)
    sched.run(until=1.0)
    before = channel.packet_in_count

    sched.schedule(1.3 - sched.now, h1.ping, h2.mac, 3, h2.ip)
    sched.run(until=1.5)

    assert channel.packet_in_count == before   # cả hai flow vẫn sống
