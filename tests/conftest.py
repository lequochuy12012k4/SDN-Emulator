import pytest

from sdnsim.core.scheduler import Scheduler
from sdnsim.core.host import Host
from sdnsim.core.switch import Switch
from sdnsim.core.link import Link
from sdnsim.control.channel import Channel
from sdnsim.control.controllers.learning import LearningController


@pytest.fixture
def two_switch_net():
    sched = Scheduler()
    channel = Channel(sched)
    channel.controller = LearningController()
    s1 = Switch(sched, "s1", channel)
    s2 = Switch(sched, "s2", channel)
    h1 = Host(sched, "h1", "00:00:00:00:00:01", "10.0.0.1")
    h2 = Host(sched, "h2", "00:00:00:00:00:02", "10.0.0.2")
    h3 = Host(sched, "h3", "00:00:00:00:00:03", "10.0.0.3")
    Link(sched, h1, 1, s1, 1)
    Link(sched, h2, 1, s1, 2)
    Link(sched, s1, 3, s2, 1)
    Link(sched, h3, 1, s2, 2)
    return sched, channel, h1, h2, h3