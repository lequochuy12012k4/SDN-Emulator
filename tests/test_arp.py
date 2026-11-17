import pytest


def ping_ip_and_run(sched, src, dst_ip, seq):
    sched.schedule(0.1, src.ping_ip, dst_ip, seq)
    sched.run()


def test_arp_resolves_mac_then_pings(two_switch_net):
    sched, channel, h1, h2, h3 = two_switch_net
    ping_ip_and_run(sched, h1, "10.0.0.3", 1)

    assert h1.arp_table == {"10.0.0.3": h3.mac}
    assert h1.rtts == [pytest.approx(0.008)]
    # ARP: 2 PACKET_IN mỗi switch (request + reply); ICMP: thêm 1 mỗi switch
    assert channel.packet_in_by_switch == {"s1": 3, "s2": 3}


def test_second_ping_uses_cache_and_flows(two_switch_net):
    sched, channel, h1, h2, h3 = two_switch_net
    ping_ip_and_run(sched, h1, "10.0.0.3", 1)
    before = dict(channel.packet_in_by_switch)

    ping_ip_and_run(sched, h1, "10.0.0.3", 2)

    assert channel.packet_in_by_switch == before
    assert h1.rtts[1] == pytest.approx(0.006)


def test_bystander_does_not_learn_or_reply(two_switch_net):
    sched, channel, h1, h2, h3 = two_switch_net
    ping_ip_and_run(sched, h1, "10.0.0.3", 1)

    assert h2.arp_table == {}
    assert h2.rtts == []