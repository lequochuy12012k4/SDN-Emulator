from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sdnsim.api.app import create_app

TOPOLOGY = Path(__file__).resolve().parent.parent / "topologies" / "two_switch.json"


def test_ping_then_snapshot():
    app = create_app(TOPOLOGY, autorun=False)   # không chạy vòng lặp nền
    client = TestClient(app)

    assert client.post("/ping", json={"src": "h1", "dst": "h3"}).status_code == 200
    app.state.sched.run()                       # tự tiến đồng hồ trong test

    snap = client.get("/snapshot").json()
    assert snap["hosts"]["h1"]["rtts"] == [pytest.approx(0.008)]
    assert snap["packet_in_by_switch"] == {"s1": 3, "s2": 3}


def test_ping_unknown_host_returns_404():
    client = TestClient(create_app(TOPOLOGY, autorun=False))
    assert client.post("/ping", json={"src": "h1", "dst": "s1"}).status_code == 404

def test_index_page_served():
    client = TestClient(create_app(TOPOLOGY, autorun=False))
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_websocket_pushes_snapshot():
    app = create_app(TOPOLOGY, autorun=False, push_interval=0.01)
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        data = ws.receive_json()
    assert set(data["switches"]) == {"s1", "s2"}