import pytest

from sdnsim.core.scheduler import Scheduler
from sdnsim.core.host import Host
from sdnsim.core.switch import Switch
from sdnsim.core.link import Link
from sdnsim.control.channel import Channel
from sdnsim.control.controllers.learning import LearningController


def build():
    sched = Scheduler()
    channel = Channel(sched)
    channel.controller = LearningController()
    s1 = Switch(sched, "s1", channel)
    s2 = Switch(sched, "s2", channel)
    h1 = Host(sched, "h1", "00:00:00:00:00:01")
    h2 = Host(sched, "h2", "00:00:00:00:00:02")
    h3 = Host(sched, "h3", "00:00:00:00:00:03")
    Link(sched, h1, 1, s1, 1)
    Link(sched, h2, 1, s1, 2)
    Link(sched, s1, 3, s2, 1)
    Link(sched, h3, 1, s2, 2)
    return sched, channel, h1, h2, h3


def ping_and_run(sched, src, dst, seq):
    sched.schedule(0.1, src.ping, dst.mac, seq)
    sched.run()


def test_packet_in_per_switch_over_three_pings():
    sched, channel, h1, h2, h3 = build()

    ping_and_run(sched, h1, h3, 1)
    assert channel.packet_in_by_switch == {"s1": 2, "s2": 2}

    ping_and_run(sched, h1, h3, 2)
    assert channel.packet_in_by_switch == {"s1": 3, "s2": 3}

    ping_and_run(sched, h1, h3, 3)
    assert channel.packet_in_by_switch == {"s1": 3, "s2": 3}  # không đổi


def test_rtt_over_two_switches():
    sched, channel, h1, h2, h3 = build()
    for seq in (1, 2, 3):
        ping_and_run(sched, h1, h3, seq)

    assert h1.rtts == [pytest.approx(0.010), pytest.approx(0.008), pytest.approx(0.006)]


def test_bystander_host_ignores_flooded_packet():
    sched, channel, h1, h2, h3 = build()
    ping_and_run(sched, h1, h3, 1)

    assert h2.rtts == []