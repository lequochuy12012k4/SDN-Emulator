from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sdnsim.api.app import create_app

TOPOLOGY = Path(__file__).resolve().parent / "fixtures" / "two_switch_three_host.json"


def test_ping_with_count_sends_that_many_pings_spaced_by_interval():
    app = create_app(TOPOLOGY, autorun=False)
    client = TestClient(app)

    response = client.post("/ping", json={"src": "h1", "dst": "h3", "count": 5, "interval": 0.01})
    assert response.status_code == 200
    assert response.json() == {"seq": 1, "count": 5}

    app.state.sched.run()
    snap = client.get("/snapshot").json()
    assert len(snap["hosts"]["h1"]["rtts"]) == 5


def test_sequence_numbers_continue_across_requests():
    client = TestClient(create_app(TOPOLOGY, autorun=False))
    first = client.post("/ping", json={"src": "h1", "dst": "h3", "count": 3}).json()
    second = client.post("/ping", json={"src": "h1", "dst": "h3"}).json()
    assert (first["seq"], second["seq"]) == (1, 4)


@pytest.mark.parametrize("bad", [
    {"count": 0}, {"count": 1001}, {"count": 1.5}, {"interval": 0}, {"interval": -1}, {"interval": 61},
])
def test_invalid_burst_parameters_are_rejected(bad):
    client = TestClient(create_app(TOPOLOGY, autorun=False))
    response = client.post("/ping", json={"src": "h1", "dst": "h3", **bad})
    assert response.status_code == 422