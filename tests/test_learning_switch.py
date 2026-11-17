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
    h1 = Host(sched, "h1", "00:00:00:00:00:01")
    h2 = Host(sched, "h2", "00:00:00:00:00:02")
    Link(sched, h1, 1, s1, 1)
    Link(sched, h2, 1, s1, 2)
    return sched, channel, h1, h2


def ping_and_run(sched, h1, h2, seq):
    sched.schedule(0.1, h1.ping, h2.mac, seq)
    sched.run()


def test_third_ping_causes_no_packet_in():
    sched, channel, h1, h2 = build()

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
    sched, channel, h1, h2 = build()
    for seq in (1, 2, 3):
        ping_and_run(sched, h1, h2, seq)

    assert h1.rtts == [pytest.approx(0.006), pytest.approx(0.005), pytest.approx(0.004)]