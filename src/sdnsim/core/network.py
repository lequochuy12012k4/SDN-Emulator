"""Network builder: dựng topology từ mô tả khai báo (dict hoặc file JSON)."""

from __future__ import annotations

import json
from typing import Any

from sdnsim.core.scheduler import Scheduler
from sdnsim.core.host import Host
from sdnsim.core.switch import Switch
from sdnsim.core.link import Link
from sdnsim.control.channel import Channel
from sdnsim.control.controllers.learning import LearningController


def build_network(
    spec: dict[str, Any],
    *,
    scheduler: Scheduler | None = None,
    channel: Channel | None = None,
    controller: LearningController | None = None,
) -> tuple[Scheduler, Channel, dict[str, Host | Switch]]:
    """
    Dựng mạng từ dict mô tả.

    Các khoá của spec (đều tùy chọn):
      - "hosts":    [{"name": str, "mac": str, "ip": str}, ...]
      - "switches": [{"name": str}, ...]
      - "links":    [{"a": str, "a_port": int, "b": str, "b_port": int,
                      "delay": float (mặc định 0.001)}, ...]
      - "controller_idle_timeout": float | None
          Chỉ áp dụng khi build_network tự tạo controller.
      - "channel_latency": float (mặc định 0.001)
          Chỉ áp dụng khi build_network tự tạo channel.

    Trả về (scheduler, channel, nodes) với nodes là dict {tên: Host | Switch}.
    Ném ValueError nếu spec sai (trùng tên, node không tồn tại,
    cổng bị dùng hai lần, host dùng cổng khác 1).
    """
    sched = scheduler if scheduler is not None else Scheduler()
    ch = channel if channel is not None else Channel(
        sched, spec.get("channel_latency", 0.001), spec.get("channel_down_latency", 0.0)
    )
    ctrl = controller if controller is not None else LearningController(
        idle_timeout=spec.get("controller_idle_timeout")
    )
    ch.controller = ctrl

    nodes: dict[str, Host | Switch] = {}
    used_ports: set[tuple[str, int]] = set()

    def add(node: Host | Switch) -> None:
        if node.name in nodes:
            raise ValueError(f"tên node bị trùng: {node.name!r}")
        nodes[node.name] = node

    for h in spec.get("hosts", []):
        add(Host(sched, h["name"], h["mac"], h.get("ip")))
    for s in spec.get("switches", []):
        add(Switch(sched, s["name"], ch))

    def endpoint(name: str, port: int) -> Host | Switch:
        if name not in nodes:
            raise ValueError(f"link tham chiếu node không tồn tại: {name!r}")
        if isinstance(nodes[name], Host) and port != 1:
            raise ValueError(f"host {name!r} chỉ có cổng 1, nhận cổng {port}")
        if (name, port) in used_ports:
            raise ValueError(f"cổng {port} của {name!r} đã được dùng")
        used_ports.add((name, port))
        return nodes[name]

    for lnk in spec.get("links", []):
        a = endpoint(lnk["a"], lnk["a_port"])
        b = endpoint(lnk["b"], lnk["b_port"])
        Link(sched, a, lnk["a_port"], b, lnk["b_port"], lnk.get("delay", 0.001))

    return sched, ch, nodes


def load_network_from_json(
    path: str,
    *,
    scheduler: Scheduler | None = None,
    channel: Channel | None = None,
    controller: LearningController | None = None,
) -> tuple[Scheduler, Channel, dict[str, Host | Switch]]:
    """Đọc spec từ file JSON rồi dựng mạng."""
    with open(path, encoding="utf-8") as f:
        spec = json.load(f)
    return build_network(spec, scheduler=scheduler, channel=channel, controller=controller)