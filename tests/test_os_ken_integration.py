"""Kiểm thử tích hợp với một controller OpenFlow THẬT (os-ken) chạy ở tiến trình riêng.

Khác với controller giả trong openflow_support.py (dùng chính codec của ta ở cả hai đầu),
controller này do os-ken cài đặt độc lập, nên đây là phép thử ăn khớp giao thức thật.
Bỏ qua nếu chưa cài os-ken hoặc thiếu examples/os_ken_controller.py.
"""

import asyncio
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
TOPOLOGY = ROOT / "tests" / "fixtures" / "two_switch_three_host.json"

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


def test_real_os_ken_controller_drives_the_simulator():
    port = free_port()
    proc = subprocess.Popen([sys.executable, str(CONTROLLER), str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_listening(port, proc)

        async def scenario():
            app = create_app(TOPOLOGY, step=0.01, speed=20.0, openflow=("127.0.0.1", port),
                             retry_interval=0.2)
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    await poll(client, lambda s: all(s["controller"].get("switches", {}).values())
                               and bool(s["controller"].get("switches")))
                    after_each = []
                    for n in (1, 2, 3):
                        await client.post("/ping", json={"src": "h1", "dst": "h3"})
                        snap = await poll(client, lambda s, n=n: len(s["hosts"]["h1"]["rtts"]) == n)
                        after_each.append(snap)
                    return after_each

        snaps = asyncio.run(scenario())
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    first, second, third = snaps
    assert first["packet_in_by_switch"] == {"s1": 3, "s2": 3}       # ARP + ICMP qua os-ken
    assert third["packet_in_total"] == second["packet_in_total"]    # flow do os-ken cài đã đủ
    rtts = third["hosts"]["h1"]["rtts"]
    assert rtts[2] == pytest.approx(0.006)                          # không còn qua controller
    flows = third["switches"]["s1"]["flows"]
    assert flows and all({"in_port", "eth_dst", "eth_src"} == set(f["match"]) for f in flows)