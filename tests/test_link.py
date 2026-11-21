import random

import pytest

from sdnsim.core.link import Link
from sdnsim.core.packet import Packet
from sdnsim.core.scheduler import Scheduler

H1, H2 = "00:00:00:00:00:01", "00:00:00:00:00:02"
ONE_MBPS = 1_000_000
TX_50B_AT_1MBPS = 50 * 8 / ONE_MBPS            # 0,4 ms: gói ICMP/ARP có size 50/42 byte


class Node:
    def __init__(self, sched, name):
        self.sched, self.name, self.ports, self.got = sched, name, {}, []

    def receive(self, port, pkt):
        self.got.append(self.sched.now)


def pkt():
    return Packet(H1, H2, "icmp_req", seq=1)    # size = 50 byte


def make(**kwargs):
    sched = Scheduler()
    a, b = Node(sched, "a"), Node(sched, "b")
    return sched, a, b, Link(sched, a, 1, b, 1, **kwargs)


def test_without_bandwidth_only_delay_applies_and_exactly():
    sched, a, b, link = make(delay=0.002)
    link.transmit(a, pkt())
    sched.run()
    assert b.got == [0.002]                      # đúng từng bit, không có sai số làm tròn
    assert link.direction_stats(a) == {"sent_packets": 1, "sent_bytes": 50,
                                       "dropped_loss": 0, "dropped_queue": 0,
                                       "dropped_error": 0, "queued": 0}


def test_bandwidth_adds_serialization_time():
    sched, a, b, link = make(delay=0.001, bandwidth=ONE_MBPS)
    link.transmit(a, pkt())
    sched.run()
    assert b.got == [pytest.approx(0.001 + TX_50B_AT_1MBPS)]


def test_second_packet_waits_for_the_first_to_finish_transmitting():
    sched, a, b, link = make(delay=0.001, bandwidth=ONE_MBPS)
    link.transmit(a, pkt())
    link.transmit(a, pkt())
    sched.run()
    assert b.got == [pytest.approx(0.001 + TX_50B_AT_1MBPS),
                     pytest.approx(0.001 + 2 * TX_50B_AT_1MBPS)]


def test_each_direction_has_its_own_transmitter():
    sched, a, b, link = make(delay=0.001, bandwidth=ONE_MBPS)
    link.transmit(a, pkt())
    link.transmit(b, pkt())
    sched.run()
    assert a.got == b.got == [pytest.approx(0.001 + TX_50B_AT_1MBPS)]


def test_idle_link_does_not_delay_the_next_packet():
    sched, a, b, link = make(delay=0.001, bandwidth=ONE_MBPS)
    link.transmit(a, pkt())
    sched.schedule(0.01, link.transmit, a, pkt())
    sched.run()
    assert b.got[1] == pytest.approx(0.01 + 0.001 + TX_50B_AT_1MBPS)


def test_full_queue_drops_new_packets_and_recovers_after_draining():
    sched, a, b, link = make(delay=0.001, bandwidth=100_000, queue_limit=2)   # 4 ms mỗi gói
    for _ in range(5):
        link.transmit(a, pkt())
    stats = link.direction_stats(a)
    assert (stats["sent_packets"], stats["dropped_queue"], stats["queued"]) == (2, 3, 2)
    sched.run()
    assert len(b.got) == 2
    assert link.direction_stats(a)["queued"] == 0
    link.transmit(a, pkt())                       # hàng đợi đã trống: nhận lại được
    assert link.direction_stats(a)["dropped_queue"] == 3


def test_queue_slot_is_freed_when_a_packet_finishes_transmitting():
    sched, a, b, link = make(delay=0.001, bandwidth=100_000, queue_limit=2)
    link.transmit(a, pkt())                       # phát xong lúc 4 ms
    link.transmit(a, pkt())                       # phát xong lúc 8 ms
    sched.schedule(0.005, link.transmit, a, pkt())   # lúc 5 ms chỉ còn 1 gói trong hàng đợi
    sched.run()
    assert link.direction_stats(a)["dropped_queue"] == 0
    assert len(b.got) == 3


def test_lost_packet_is_never_delivered_but_still_occupies_the_transmitter():
    sched, a, b, link = make(delay=0.001, bandwidth=100_000, loss=1.0)
    link.transmit(a, pkt())
    link.transmit(a, pkt())
    assert link.direction_stats(a)["queued"] == 2          # cả hai còn chiếm bộ phát
    sched.run()
    assert b.got == []
    stats = link.direction_stats(a)
    assert (stats["sent_packets"], stats["dropped_loss"]) == (2, 2)


def delivered_pattern(seed, loss=0.5, n=200):
    sched, a, b, link = make(delay=0.001, loss=loss, rng=random.Random(seed))
    for i in range(n):
        sched.schedule(i * 0.01, link.transmit, a, Packet(H1, H2, "icmp_req", seq=i))
    sched.run()
    return len(b.got), link.direction_stats(a)["dropped_loss"]


def test_random_loss_is_reproducible_for_the_same_seed():
    first, second = delivered_pattern("x:0"), delivered_pattern("x:0")
    assert first == second
    delivered, dropped = first
    assert delivered + dropped == 200
    assert 60 < dropped < 140                       # xấp xỉ 50% của 200 gói


def test_different_seeds_give_different_loss_patterns():
    assert delivered_pattern("x:0") != delivered_pattern("y:0")


@pytest.mark.parametrize("kwargs", [
    {"delay": -1}, {"delay": "1"}, {"bandwidth": 0}, {"bandwidth": -5}, {"bandwidth": "10Mbps"},
    {"loss": -0.1}, {"loss": 1.5}, {"loss": "0.5"},
    {"error_rate": -0.1}, {"error_rate": 1.5}, {"error_rate": "0.5"},
    {"error_rate": True},
    {"queue_limit": 3},                                      # cần bandwidth
    {"bandwidth": 1000, "queue_limit": 0}, {"bandwidth": 1000, "queue_limit": 2.5},
])
def test_invalid_parameters_are_rejected(kwargs):
    with pytest.raises(ValueError):
        make(**kwargs)