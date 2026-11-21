"""Băng thông, hàng đợi, loss/frame error và ARP, đo qua cả mạng."""

import itertools
import json

import pytest

from sdnsim.metrics.collector import Metrics
from sdnsim.core.packet import Packet
from sdnsim.topology.network import build_network, parse_bandwidth

HOSTS = [
    {"name": "h1", "mac": "00:00:00:00:00:01", "ip": "10.0.0.1"},
    {"name": "h2", "mac": "00:00:00:00:00:02", "ip": "10.0.0.2"},
]


def spec(**first_link):
    """h1 - s1 - h2; các tham số truyền vào áp dụng cho link h1-s1."""
    return {
        "hosts": HOSTS,
        "switches": [{"name": "s1"}],
        "links": [
            {"a": "h1", "a_port": 1, "b": "s1", "b_port": 1, **first_link},
            {"a": "h2", "a_port": 1, "b": "s1", "b_port": 2},
        ],
    }


class SequenceRng:
    """Nguồn ngẫu nhiên giả: trả lần lượt các giá trị cho trước, sau đó luôn trả 0.9."""

    def __init__(self, first_values):
        self._values = itertools.chain(first_values, itertools.repeat(0.9))

    def random(self):
        return next(self._values)


def h1_link_stats(sched, channel, nodes):
    snapshot = Metrics(sched, channel, nodes).snapshot()
    return next(l for l in snapshot["links"] if l["a"] == "h1")


# --------------------------------------------------------------- đọc spec

@pytest.mark.parametrize("value, expected", [
    (None, None), (1_000_000, 1e6), (2.5, 2.5),
    ("500Kbps", 5e5), ("500 kbps", 5e5), ("10Mbps", 1e7), ("1.5Gbps", 1.5e9), ("64bps", 64.0),
])
def test_parse_bandwidth(value, expected):
    assert parse_bandwidth(value) == expected


@pytest.mark.parametrize("bad", ["1000", "fast", "10 parsecs", "Mbps", "-5Mbps", "0Mbps", 0, -5, True, [1]])
def test_parse_bandwidth_rejects_bad_values(bad):
    with pytest.raises(ValueError):
        parse_bandwidth(bad)


def test_link_parameters_from_spec_reach_the_link():
    _, _, nodes = build_network(spec(
        delay=0.002, bandwidth="10Mbps", loss=0.1, error_rate=0.2, queue_limit=5))
    link = nodes["h1"].ports[1]
    assert (link.delay, link.bandwidth, link.loss, link.error_rate, link.queue_limit) == (
        0.002, 1e7, 0.1, 0.2, 5)
    other = nodes["h2"].ports[1]                    # link không khai báo gì: mặc định cũ
    assert (other.delay, other.bandwidth, other.loss, other.error_rate, other.queue_limit) == (
        0.001, None, 0.0, 0.0, None)


def test_invalid_link_parameter_in_spec_is_rejected():
    for bad in (
        {"loss": 2}, {"error_rate": 2}, {"bandwidth": "fast"},
        {"queue_limit": 3}, {"delay": -1},
    ):
        with pytest.raises(ValueError):
            build_network(spec(**bad))


def first_random(seed):
    _, _, nodes = build_network({**spec(), "seed": seed})
    return nodes["h1"].ports[1].rng.random()


def test_seed_makes_each_links_randomness_reproducible():
    assert first_random(7) == first_random(7)
    assert first_random(7) != first_random(8)
    _, _, nodes = build_network({**spec(), "seed": 7})
    assert nodes["h1"].ports[1].rng.random() != nodes["h2"].ports[1].rng.random()   # mỗi link một luồng


# --------------------------------------------------- hành vi qua cả mạng

def test_default_network_keeps_the_original_rtts():
    """Không khai báo băng thông/mất gói thì kết quả phải y hệt trước khi có tính năng mới."""
    sched, ch, nodes = build_network(spec())
    for seq in (1, 2, 3):
        sched.schedule(0.1, nodes["h1"].ping, "00:00:00:00:00:02", seq, "10.0.0.2")
        sched.run()
    assert nodes["h1"].rtts == [pytest.approx(0.006), pytest.approx(0.005), pytest.approx(0.004)]


def test_queue_builds_up_and_overflow_is_dropped_under_a_burst():
    sched, ch, nodes = build_network(spec(bandwidth=100_000, queue_limit=3))     # 4 ms mỗi gói
    for i in range(20):
        sched.schedule(i * 0.001, nodes["h1"].ping_ip, "10.0.0.2", i + 1)
    sched.run()

    stats = h1_link_stats(sched, ch, nodes)["a_to_b"]
    rtts = nodes["h1"].rtts
    assert stats["dropped_queue"] > 0
    assert stats["sent_packets"] + stats["dropped_queue"] == 21         # 1 ARP + 20 ping
    assert 0 < len(rtts) < 20                                           # không phải ping nào cũng qua
    assert rtts[1] - rtts[0] == pytest.approx(0.004)                    # gói sau xếp hàng sau gói trước
    assert rtts[2] - rtts[1] == pytest.approx(0.004)


def test_lost_arp_is_retried_and_the_ping_still_completes():
    sched, ch, nodes = build_network(spec(loss=0.5))
    link = nodes["h1"].ports[1]
    link.rng = SequenceRng([0.0])                    # gói đầu bị mất, các gói sau thì không
    nodes["h1"].ping_ip("10.0.0.2", 1)
    sched.run()

    assert len(nodes["h1"].rtts) == 1
    assert nodes["h1"].arp_failures == 0
    assert h1_link_stats(sched, ch, nodes)["a_to_b"]["dropped_loss"] == 1   # chỉ ARP request đầu bị mất


def test_link_error_is_counted_on_the_receiving_switch_port():
    sched, _, nodes = build_network(spec(error_rate=1.0))
    nodes["h1"].send(Packet("00:00:00:00:00:01", "ff:ff:ff:ff:ff:ff", "arp_req"))
    assert nodes["s1"].port_stats[1]["rx_errors"] == 0
    sched.run()

    stats = nodes["s1"].port_stats[1]
    link_stats = nodes["h1"].ports[1].direction_stats(nodes["h1"])
    assert (stats["rx_packets"], stats["rx_dropped"], stats["rx_errors"], stats["rx_crc_err"]) == (
        0, 1, 1, 1)
    assert link_stats["dropped_error"] == 1


def test_ping_is_abandoned_after_three_lost_arp_requests():
    sched, ch, nodes = build_network(spec(loss=1.0))
    h1 = nodes["h1"]
    h1.ping_ip("10.0.0.2", 1)

    # Chạy theo mốc giờ ảo (không dùng run() không giới hạn): nếu việc bỏ cuộc hỏng thì test
    # báo lỗi chứ không treo.
    sched.run(until=2.5)                             # đã hỏi lúc 0, 1 và 2 giây
    assert h1.pending == {"10.0.0.2": [1]} and h1.arp_failures == 0
    assert h1_link_stats(sched, ch, nodes)["a_to_b"]["sent_packets"] == 3

    sched.run(until=3.5)                             # lúc 3 giây hết lượt hỏi nên bỏ cuộc
    assert h1.rtts == [] and h1.pending == {} and h1.arp_failures == 1
    stats = h1_link_stats(sched, ch, nodes)["a_to_b"]
    assert (stats["sent_packets"], stats["dropped_loss"]) == (3, 3)      # không hỏi thêm lần thứ tư


def test_pings_waiting_for_one_arp_share_a_single_request():
    sched, ch, nodes = build_network(spec())
    for seq in range(1, 6):
        nodes["h1"].ping_ip("10.0.0.2", seq)
    sched.run()
    assert len(nodes["h1"].rtts) == 5                # cả năm ping đều được gửi sau ARP reply
    assert h1_link_stats(sched, ch, nodes)["a_to_b"]["sent_packets"] == 6   # 1 ARP + 5 ICMP


def test_snapshot_exposes_link_settings_and_counters_as_json():
    sched, ch, nodes = build_network(
        spec(bandwidth="1Mbps", loss=0.0, error_rate=0.1, queue_limit=4))
    nodes["h1"].ping_ip("10.0.0.2", 1)
    sched.run()
    snapshot = json.loads(json.dumps(Metrics(sched, ch, nodes).snapshot()))
    link = next(l for l in snapshot["links"] if l["a"] == "h1")
    assert (link["bandwidth"], link["loss"], link["error_rate"], link["queue_limit"]) == (
        1e6, 0.0, 0.1, 4)
    assert link["a_to_b"]["sent_packets"] >= 1 and link["b_to_a"]["sent_packets"] >= 1
    assert snapshot["hosts"]["h1"]["arp_failures"] == 0