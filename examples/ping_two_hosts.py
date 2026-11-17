from sdnsim.core.scheduler import Scheduler
from sdnsim.core.host import Host
from sdnsim.core.switch import Switch
from sdnsim.core.link import Link
from sdnsim.control.channel import Channel
from sdnsim.control.controllers.learning import LearningController

sched = Scheduler()
channel = Channel(sched)
channel.controller = LearningController()

s1 = Switch(sched, "s1", channel)
h1 = Host(sched, "h1", "00:00:00:00:00:01")
h2 = Host(sched, "h2", "00:00:00:00:00:02")
Link(sched, h1, 1, s1, 1)
Link(sched, h2, 1, s1, 2)

for i, t in enumerate([0.0, 0.1, 0.2], start=1):
    sched.schedule(t, h1.ping, h2.mac, i)
sched.run()