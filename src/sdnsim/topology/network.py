"""Network builder: dựng topology từ mô tả khai báo (dict hoặc JSON)."""

from __future__ import annotations

import json
import random
import re
from typing import Any

from sdnsim.control.channel import Channel
from sdnsim.control.controllers.learning import LearningController
from sdnsim.core.host import Host
from sdnsim.core.link import Link
from sdnsim.core.scheduler import Scheduler
from sdnsim.core.switch import Switch

_BANDWIDTH_UNITS = {"bps": 1.0, "kbps": 1e3, "mbps": 1e6, "gbps": 1e9}


def parse_bandwidth(value: Any) -> float | None:
    """Số (bit/giây) hoặc chuỗi có đơn vị như "500Kbps", "10Mbps", "1Gbps"; None = không giới hạn."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"băng thông không hợp lệ: {value!r}")
    if isinstance(value, (int, float)):
        if value <= 0:
            raise ValueError(f"băng thông phải dương: {value!r}")
        return float(value)
    if isinstance(value, str):
        match = re.fullmatch(r"\s*(\d*\.?\d+)\s*([A-Za-z]+)\s*", value)
        if match and match.group(2).lower() in _BANDWIDTH_UNITS:
            result = float(match.group(1)) * _BANDWIDTH_UNITS[match.group(2).lower()]
            if result > 0:
                return result
    raise ValueError(f"băng thông không hợp lệ: {value!r} (ví dụ hợp lệ: 1000000, \"10Mbps\")")


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
                      "delay": float (giây, mặc định 0.001),
                      "bandwidth": số bit/giây hoặc chuỗi như "10Mbps" (mặc định không giới hạn),
                      "loss": xác suất mất gói 0..1 (mặc định 0),
                      "queue_limit": số gói tối đa trong hàng đợi mỗi chiều (cần bandwidth)}, ...]
      - "seed": số hoặc chuỗi làm hạt giống cho việc mất gói ngẫu nhiên (mặc định 0).
          Mỗi link có một nguồn ngẫu nhiên riêng, nên cùng seed thì kết quả luôn giống nhau.
      - "controller_idle_timeout": float | None
          Chỉ áp dụng khi build_network tự tạo controller.
      - "channel_latency": float (mặc định 0.001), "channel_down_latency": float (mặc định 0)
          Chỉ áp dụng khi build_network tự tạo channel.

    Nếu truyền `channel` mà không truyền `controller`, không tạo controller nội bộ
    (kênh đó tự lo phía controller, ví dụ OpenFlowChannel).

    Trả về (scheduler, channel, nodes) với nodes là dict {tên: Host | Switch}.
    Ném ValueError nếu spec sai (trùng tên, node không tồn tại, cổng bị dùng hai lần,
    host dùng cổng khác 1, thông số link không hợp lệ).
    """
    sched = scheduler if scheduler is not None else Scheduler()
    ch = channel if channel is not None else Channel(
        sched, spec.get("channel_latency", 0.001), spec.get("channel_down_latency", 0.0)
    )
    if controller is None and channel is not None:
        ctrl = None
    else:
        ctrl = controller if controller is not None else LearningController(
            idle_timeout=spec.get("controller_idle_timeout"))
    if ctrl is not None:
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

    seed = spec.get("seed", 0)
    for index, lnk in enumerate(spec.get("links", [])):
        a = endpoint(lnk["a"], lnk["a_port"])
        b = endpoint(lnk["b"], lnk["b_port"])
        Link(sched, a, lnk["a_port"], b, lnk["b_port"], lnk.get("delay", 0.001),
             bandwidth=parse_bandwidth(lnk.get("bandwidth")),
             loss=lnk.get("loss", 0.0),
             queue_limit=lnk.get("queue_limit"),
             rng=random.Random(f"{seed}:{index}"))

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