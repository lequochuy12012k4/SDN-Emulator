"""Kiểm thử tích hợp với một controller OpenFlow THẬT (os-ken) chạy ở tiến trình riêng.

Khác với controller giả trong openflow_support.py (dùng chính codec của ta ở cả hai đầu),
controller này do os-ken cài đặt độc lập, nên đây là phép thử ăn khớp giao thức thật.
Bỏ qua nếu chưa cài os-ken hoặc thiếu examples/os_ken_controller.py.
"""

import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

pytest.importorskip("os_ken")

from sdnsim.api.app import create_app                                   # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONTROLLER = ROOT / "examples" / "os_ken_controller.py"
TOPOLOGY = ROOT / "topologies" / "two_switch_three_host.json"

pytestmark = pytest.mark.skipif(not CONTROLLER.exists(), reason="thiếu examples/os_ken_controller.py")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_listening(port, proc, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"controller os-ken thoát sớm (mã {proc.returncode})")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError("controller os-ken không mở cổng trong thời hạn")


@contextlib.contextmanager
def os_ken_controller(port, extra_env=None, args=()):
    """Chạy examples/os_ken_controller.py ở tiến trình riêng, dừng nó khi thoát khối with."""
    proc = subprocess.Popen(
        [sys.executable, str(CONTROLLER), str(port), *map(str, args)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={**os.environ, **(extra_env or {})})
    try:
        wait_listening(port, proc)
        yield proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


async def poll(client, ok, timeout=15.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        snap = (await client.get("/snapshot")).json()
        if ok(snap):
            return snap
        if loop.time() > deadline:
            raise TimeoutError(f"điều kiện không đạt; controller: {snap['controller']}")
        await asyncio.sleep(0.05)


def all_connected(snap):
    switches = snap["controller"].get("switches", {})
    return bool(switches) and all(switches.values())


async def ping_three_times(client):
    snaps = []
    for n in (1, 2, 3):
        await client.post("/ping", json={"src": "h1", "dst": "h3"})
        snaps.append(await poll(client, lambda s, n=n: len(s["hosts"]["h1"]["rtts"]) == n))
    return snaps


def test_real_os_ken_controller_drives_the_simulator():
    port = free_port()
    with os_ken_controller(port):
        async def scenario():
            app = create_app(TOPOLOGY, step=0.01, speed=20.0, openflow=("127.0.0.1", port),
                             retry_interval=0.2)
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    await poll(client, all_connected)
                    return await ping_three_times(client)

        snaps = asyncio.run(scenario())

    first, second, third = snaps
    assert first["packet_in_by_switch"] == {"s1": 3, "s2": 3}       # ARP + ICMP qua os-ken
    assert third["packet_in_total"] == second["packet_in_total"]    # flow do os-ken cài đã đủ
    rtts = third["hosts"]["h1"]["rtts"]
    assert rtts[2] == pytest.approx(0.006)                          # không còn qua controller
    flows = third["switches"]["s1"]["flows"]
    assert flows and all({"in_port", "eth_dst", "eth_src"} == set(f["match"]) for f in flows)


def read_log(path):
    """Trả về (bản ghi cổng cuối, bản ghi flow cuối) của switch có dpid 1 (s1), hoặc None."""
    if not path.exists():
        return None, None
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    last = lambda kind: next((r for r in reversed(records) if r["kind"] == kind and r["dpid"] == 1), None)  # noqa: E731
    return last("port"), last("flow")


def monitor_agrees_with_snapshot(snap, port_record, flow_record):
    if port_record is None or flow_record is None:
        return False
    s1 = snap["switches"]["s1"]
    seen_ports = {p["port_no"]: (p["rx_packets"], p["tx_packets"], p["rx_bytes"], p["tx_bytes"])
                  for p in port_record["ports"]}
    real_ports = {int(p): (c["rx_packets"], c["tx_packets"], c["rx_bytes"], c["tx_bytes"])
                  for p, c in s1["ports"].items()}
    if any(seen_ports.get(p) != counters for p, counters in real_ports.items()):
        return False
    seen_flows = {tuple(sorted(f["match"].items())): (f["packet_count"], f["byte_count"], f["idle_timeout"])
                  for f in flow_record["flows"]}
    real_flows = {tuple(sorted(f["match"].items())): (f["packets"], f["bytes"], int(f["idle_timeout"] or 0))
                  for f in s1["flows"]}
    return bool(real_flows) and seen_flows == real_flows


def test_real_os_ken_monitor_reads_the_same_counters_as_the_dashboard(tmp_path):
    log = tmp_path / "stats.jsonl"
    port = free_port()
    env = {"SDNSIM_OSKEN_STATS_LOG": str(log), "SDNSIM_OSKEN_STATS_INTERVAL": "0.3"}
    with os_ken_controller(port, env, args=(30,)):                  # idle_timeout 30 s: flow không hết hạn giữa chừng
        async def scenario():
            app = create_app(TOPOLOGY, step=0.01, speed=20.0, openflow=("127.0.0.1", port),
                             retry_interval=0.2)
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    await poll(client, all_connected)
                    await ping_three_times(client)
                    # Ngừng lưu lượng rồi chờ tới khi lần hỏi gần nhất của os-ken khớp với snapshot.
                    loop = asyncio.get_running_loop()
                    deadline = loop.time() + 15
                    while True:
                        snap = (await client.get("/snapshot")).json()
                        ports, flows = read_log(log)
                        if monitor_agrees_with_snapshot(snap, ports, flows):
                            return snap, ports, flows
                        if loop.time() > deadline:
                            raise TimeoutError(f"os-ken thấy {ports} / {flows}, dashboard thấy {snap['switches']['s1']}")
                        await asyncio.sleep(0.1)

        snap, ports, flows = asyncio.run(scenario())

    assert {p["port_no"] for p in ports["ports"]} >= {1, 2, 3}      # os-ken thấy mọi cổng đã nối của s1
    assert sum(p["rx_packets"] for p in ports["ports"]) > 0
    assert all(f["idle_timeout"] == 30 for f in flows["flows"])     # timeout do chính os-ken đặt