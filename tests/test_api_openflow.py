"""API ở chế độ OpenFlow: switch mô phỏng kết nối tới một controller bên ngoài."""

import asyncio
import socket
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from openflow_support import FakeController
from sdnsim.api.app import create_app

TOPOLOGY = Path(__file__).resolve().parent / "fixtures" / "two_switch_three_host.json"


async def poll(client, ok, timeout=8.0):
    """Hỏi /snapshot tới khi điều kiện đúng; trả về snapshot cuối."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        snap = (await client.get("/snapshot")).json()
        if ok(snap):
            return snap
        if loop.time() > deadline:
            raise TimeoutError(f"điều kiện không đạt; snapshot cuối: {snap['controller']}")
        await asyncio.sleep(0.02)


def all_connected(snap):
    c = snap["controller"]
    return c["mode"] == "openflow" and bool(c["switches"]) and all(c["switches"].values())


def test_internal_mode_reports_internal_controller():
    client = TestClient(create_app(TOPOLOGY, autorun=False))
    assert client.get("/snapshot").json()["controller"] == {"mode": "internal"}


def test_openflow_mode_connects_and_pings_through_external_controller():
    async def scenario():
        controller = FakeController()
        await controller.start()
        app = create_app(TOPOLOGY, step=0.01, speed=20.0,
                         openflow=("127.0.0.1", controller.port))
        try:
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    await poll(client, all_connected)
                    response = await client.post("/ping", json={"src": "h1", "dst": "h3"})
                    assert response.status_code == 200
                    return await poll(client, lambda s: len(s["hosts"]["h1"]["rtts"]) == 1)
        finally:
            await controller.stop()

    snap = asyncio.run(scenario())
    assert snap["controller"]["switches"] == {"s1": True, "s2": True}
    assert snap["packet_in_total"] >= 2           # ARP và ICMP đều phải hỏi controller
    flows = snap["switches"]["s1"]["flows"]
    assert flows and all("in_port" in f["match"] for f in flows)   # flow do controller cài


def test_connection_is_retried_until_the_controller_appears():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]                 # cổng trống: chưa ai nghe

    async def scenario():
        app = create_app(TOPOLOGY, step=0.01, speed=20.0,
                         openflow=("127.0.0.1", port), retry_interval=0.05)
        controller = FakeController()
        try:
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    await asyncio.sleep(0.2)
                    before = (await client.get("/snapshot")).json()["controller"]
                    await controller.start(port)  # bây giờ controller mới xuất hiện
                    await poll(client, all_connected)
                    return before
        finally:
            await controller.stop()

    before = asyncio.run(scenario())
    assert before["mode"] == "openflow"
    assert not any(before["switches"].values())   # lúc đầu chưa switch nào kết nối được


def test_switches_reconnect_after_the_controller_restarts():
    async def scenario():
        controller = FakeController()
        await controller.start()
        port = controller.port
        restarted_controller = FakeController()
        app = create_app(TOPOLOGY, step=0.01, speed=20.0,
                         openflow=("127.0.0.1", port), retry_interval=0.05)
        controller_running = True
        restarted_running = False
        try:
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    await poll(client, all_connected)
                    await controller.stop()
                    controller_running = False
                    await poll(
                        client,
                        lambda s: bool(s["controller"]["switches"])
                        and not any(s["controller"]["switches"].values()),
                    )

                    await restarted_controller.start(port)
                    restarted_running = True
                    await poll(client, all_connected)
                    response = await client.post("/ping", json={"src": "h1", "dst": "h3"})
                    assert response.status_code == 200
                    return await poll(client, lambda s: len(s["hosts"]["h1"]["rtts"]) == 1)
        finally:
            if controller_running:
                await controller.stop()
            if restarted_running:
                await restarted_controller.stop()

    snapshot = asyncio.run(scenario())
    assert all(snapshot["controller"]["switches"].values())
    assert snapshot["packet_in_total"] >= 2