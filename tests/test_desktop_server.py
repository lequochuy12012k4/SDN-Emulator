import json
from pathlib import Path
import socket
import struct
import time

import pytest
from fastapi.testclient import TestClient

from sdnsim.desktop.server import create_desktop_app
from sdnsim.openflow.header import OFPT_PACKET_IN, OFPT_PACKET_OUT


TOPOLOGY = Path(__file__).resolve().parent.parent / "topologies" / "two_switch.json"
pytest.importorskip("os_ken")


def _topology_with_local_controller(tmp_path):
    with TOPOLOGY.open(encoding="utf-8") as f:
        spec = json.load(f)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    spec["controllers"] = [{
        "name": "c0", "mode": "openflow", "host": "127.0.0.1", "port": port,
        "ports": 8, "idle_timeout": None, "x": 380.0, "y": 55.0,
    }]
    spec["control_links"] = [
        {"controller": "c0", "c_port": 1, "switch": "s1"},
        {"controller": "c0", "c_port": 2, "switch": "s2"},
    ]
    path = tmp_path / "initial_topology.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path


def _wait_for(client, condition, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = client.get("/snapshot").json()
        if condition(snapshot):
            return snapshot
        time.sleep(0.02)
    raise AssertionError(f"Điều kiện không đạt trước timeout: {snapshot}")


def _all_switches_connected(snapshot):
    switches = snapshot["controller"]["switches"]
    return bool(switches) and all(switches.values())


def test_web_ui_paths_serve_templates_and_assets(tmp_path):
    app = create_desktop_app(
        topologies_dir=tmp_path / "topologies",
        autorun=False,
        autostart_osken=False,
    )
    with TestClient(app) as client:
        web_page = client.get("/")
        desktop_page = client.get("/desktop")

        assert web_page.status_code == 200
        assert "Web Dashboard" in web_page.text
        assert desktop_page.status_code == 200
        assert "Desktop Studio" in desktop_page.text

        for path, content_type in (
            ("/assets/style/index.css", "text/css"),
            ("/assets/style/desktop.css", "text/css"),
            ("/assets/script/index.js", "javascript"),
            ("/assets/script/desktop.js", "javascript"),
            ("/assets/template/icon.png", "image/png"),
        ):
            response = client.get(path)
            assert response.status_code == 200
            assert content_type in response.headers["content-type"]


def _openflow_types_from_pcap(data):
    message_types = []
    offset = 24
    while offset + 16 <= len(data):
        _, _, captured_len, _ = struct.unpack_from("<IIII", data, offset)
        offset += 16
        frame = data[offset:offset + captured_len]
        offset += captured_len
        if len(frame) < 54 or frame[12:14] != b"\x08\x00" or frame[23] != 6:
            continue
        tcp_offset = 14 + (frame[14] & 0x0F) * 4
        if tcp_offset + 20 > len(frame):
            continue
        payload_offset = tcp_offset + (frame[tcp_offset + 12] >> 4) * 4
        payload = frame[payload_offset:]
        if len(payload) >= 8 and payload[0] == 4:
            message_len = struct.unpack_from("!H", payload, 2)[0]
            if 8 <= message_len <= len(payload):
                message_types.append(payload[1])
    return message_types


def test_desktop_and_web_sync_exact_topology_and_ping(tmp_path):
    app = create_desktop_app(
        initial_topology=_topology_with_local_controller(tmp_path),
        topologies_dir=tmp_path / "topologies",
        step=0.01,
        speed=20.0,
    )
    with TestClient(app) as client:
        web_page = client.get("/")
        desktop_page = client.get("/desktop")
        assert web_page.status_code == 200
        assert desktop_page.status_code == 200
        assert "Web Dashboard" in web_page.text
        assert "Desktop Studio" in desktop_page.text
        assert client.get("/api/topology").json()["osken_running"] is True
        _wait_for(client, _all_switches_connected)

        spec = client.get("/api/topology").json()["spec"]
        spec["hosts"].append({
            "name": "h4", "mac": "00:00:00:00:00:04", "ip": "10.0.0.4",
            "ports": 1, "x": 510.0, "y": 330.0,
        })
        spec["links"].append({
            "a": "h4", "a_port": 1, "b": "s2", "b_port": 3, "delay": 0.001,
        })
        assert client.put("/api/topology", json=spec).status_code == 200
        _wait_for(client, _all_switches_connected)

        assert client.patch(
            "/api/topology/positions", json={"s2": {"x": 455.0, "y": 180.0}}
        ).status_code == 200
        snap = client.get("/snapshot").json()
        assert snap["layout"]["switches"]["s2"]["x"] == 455.0
        assert snap["layout"]["hosts"]["h4"]["x"] == 510.0
        assert any(l["a"] == "h4" and l["b"] == "s2" and l["b_port"] == 3 for l in snap["links"])

        exec_res = client.post("/api/host/h1/exec", json={"command": "ping h4"})
        assert exec_res.status_code == 200 and exec_res.json()["ok"] is True
        snap_after = _wait_for(
            client,
            lambda s: len(s["hosts"]["h1"]["rtts"]) == 1,
        )
        assert snap_after["packet_in_total"] > 0

        ctrl_logs = client.get("/api/logs?category=controller").json()["entries"]
        assert any(e["level"] == "PACKET_IN" for e in ctrl_logs)
        assert any(e["level"] == "PACKET_OUT" for e in ctrl_logs)
        assert any(e["level"] == "FLOW_MOD" for e in ctrl_logs)
        of_types = _openflow_types_from_pcap(client.get("/api/capture/pcap").content)
        assert OFPT_PACKET_IN in of_types
        assert OFPT_PACKET_OUT in of_types

def test_unlinked_switch_drops_packet_in_until_controller_linked(tmp_path):
    app = create_desktop_app(
        initial_topology=_topology_with_local_controller(tmp_path),
        topologies_dir=tmp_path / "topologies",
        step=0.01,
        speed=20.0,
    )
    with TestClient(app) as client:
        _wait_for(client, _all_switches_connected)
        spec = client.get("/api/topology").json()["spec"]
        spec["control_links"] = []
        assert client.put("/api/topology", json=spec).status_code == 200

        assert client.post("/api/host/h1/exec", json={"command": "ping h2"}).status_code == 200
        snap = _wait_for(client, lambda s: s["packet_in_total"] > 0)
        assert snap["hosts"]["h1"]["rtts"] == []
        logs = client.get("/api/logs?category=controller").json()["entries"]
        assert any("Chưa nối Controller" in e["message"] for e in logs)

        spec["control_links"] = [
            {"controller": "c0", "c_port": 1, "switch": "s1"},
            {"controller": "c0", "c_port": 2, "switch": "s2"},
        ]
        assert client.put("/api/topology", json=spec).status_code == 200
        _wait_for(client, _all_switches_connected)
        assert client.post("/api/host/h1/exec", json={"command": "ping h2"}).status_code == 200
        snap2 = _wait_for(client, lambda s: len(s["hosts"]["h1"]["rtts"]) >= 1)
        assert len(snap2["hosts"]["h1"]["rtts"]) >= 1