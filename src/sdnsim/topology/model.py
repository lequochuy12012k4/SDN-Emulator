from __future__ import annotations

from copy import deepcopy
from typing import Any

from sdnsim.control.channel import Channel
from sdnsim.core.scheduler import Scheduler
from sdnsim.topology.network import build_network


class TopologyModel:
    """Mô hình topology dùng chung cho Desktop Studio và Simulator."""

    def __init__(self, with_default_controller: bool = True) -> None:
        self.hosts: dict[str, dict[str, Any]] = {}
        self.switches: dict[str, dict[str, Any]] = {}
        self.controllers: dict[str, dict[str, Any]] = {}
        self.links: list[dict[str, Any]] = []
        self.control_links: list[dict[str, Any]] = []
        self.seed: int | None = None

        if with_default_controller:
            self.add_controller(name="c0", mode="openflow", ports=8, x=380.0, y=55.0)

    # --- Sinh tên / MAC / IP tự động ---
    def _all_names(self) -> set[str]:
        return set(self.hosts) | set(self.switches) | set(self.controllers)

    def next_host_name(self) -> str:
        i = 1
        names = self._all_names()
        while f"h{i}" in names:
            i += 1
        return f"h{i}"

    def next_switch_name(self) -> str:
        i = 1
        names = self._all_names()
        while f"s{i}" in names:
            i += 1
        return f"s{i}"

    def next_controller_name(self) -> str:
        i = 0
        names = self._all_names()
        while f"c{i}" in names:
            i += 1
        return f"c{i}"

    def next_mac(self) -> str:
        used = {h["mac"].lower() for h in self.hosts.values()}
        i = 1
        while True:
            mac = ":".join(f"{(i >> (8 * k)) & 0xFF:02x}" for k in range(5, -1, -1))
            if mac not in used:
                return mac
            i += 1

    def next_ip(self) -> str:
        used = {h["ip"] for h in self.hosts.values() if h.get("ip")}
        i = 1
        while True:
            ip = f"10.0.{(i - 1) // 254}.{(i - 1) % 254 + 1}"
            if ip not in used:
                return ip
            i += 1

    def _check_unique_name(self, name: str, ignore: str | None = None) -> None:
        if not name or not name.strip():
            raise ValueError("Tên thiết bị không được để trống")
        if name == ignore:
            return
        if name in self._all_names():
            raise ValueError(f"Tên thiết bị đã tồn tại: {name!r}")

    # --- Quản lý cổng ---
    def get_port_count(self, name: str) -> int:
        if name in self.hosts:
            return int(self.hosts[name].get("ports", 1))
        if name in self.switches:
            return int(self.switches[name].get("ports", 4))
        if name in self.controllers:
            return int(self.controllers[name].get("ports", 8))
        raise ValueError(f"Không tìm thấy thiết bị {name!r}")

    def used_ports(self, name: str) -> dict[int, str]:
        used: dict[int, str] = {}
        if name in self.controllers:
            for cl in self.control_links:
                if cl["controller"] == name:
                    used[int(cl["c_port"])] = f"{cl['switch']}:OF"
            return used

        for lnk in self.links:
            if lnk["a"] == name:
                used[int(lnk["a_port"])] = f"{lnk['b']}:{lnk['b_port']}"
            if lnk["b"] == name:
                used[int(lnk["b_port"])] = f"{lnk['a']}:{lnk['a_port']}"
        return used

    def switch_controller(self, switch_name: str) -> dict[str, Any] | None:
        for cl in self.control_links:
            if cl["switch"] == switch_name:
                return cl
        return None

    def port_list(self, name: str) -> list[dict[str, Any]]:
        total = self.get_port_count(name)
        used = self.used_ports(name)
        return [
            {"port": p, "used": p in used, "peer": used.get(p)}
            for p in range(1, total + 1)
        ]

    # --- CRUD Host ---
    def add_host(
        self,
        name: str | None = None,
        mac: str | None = None,
        ip: str | None = None,
        x: float = 160.0,
        y: float = 310.0,
    ) -> dict[str, Any]:
        name = (name or self.next_host_name()).strip()
        self._check_unique_name(name)
        mac = (mac or self.next_mac()).strip()
        ip = (ip or self.next_ip()).strip()
        host = {"name": name, "mac": mac, "ip": ip, "ports": 1, "x": float(x), "y": float(y)}
        self.hosts[name] = host
        return deepcopy(host)

    def update_host(
        self, old_name: str, name: str | None = None, mac: str | None = None, ip: str | None = None
    ) -> dict[str, Any]:
        if old_name not in self.hosts:
            raise ValueError(f"Host không tồn tại: {old_name!r}")
        cur = self.hosts[old_name]
        new_name = (name if name is not None else cur["name"]).strip()
        self._check_unique_name(new_name, ignore=old_name)
        new_mac = (mac if mac is not None else cur["mac"]).strip()
        new_ip = (ip if ip is not None else cur["ip"]).strip()
        if not new_mac or not new_ip:
            raise ValueError("MAC và IP không được để trống")

        updated = {**cur, "name": new_name, "mac": new_mac, "ip": new_ip, "ports": 1}
        if new_name != old_name:
            del self.hosts[old_name]
            self.hosts[new_name] = updated
            for lnk in self.links:
                if lnk["a"] == old_name:
                    lnk["a"] = new_name
                if lnk["b"] == old_name:
                    lnk["b"] = new_name
        else:
            self.hosts[old_name] = updated
        return deepcopy(updated)

    def delete_host(self, name: str) -> None:
        if name not in self.hosts:
            raise ValueError(f"Host không tồn tại: {name!r}")
        del self.hosts[name]
        self.links = [l for l in self.links if l["a"] != name and l["b"] != name]

    # --- CRUD Switch ---
    def add_switch(
        self,
        name: str | None = None,
        ports: int = 4,
        x: float = 280.0,
        y: float = 175.0,
        delay: float = 0.0,
        bandwidth: int | str | None = None,
        queue_limit: int | None = None,
    ) -> dict[str, Any]:
        name = (name or self.next_switch_name()).strip()
        self._check_unique_name(name)
        ports = int(ports)
        if ports < 1 or ports > 64:
            raise ValueError("Số cổng Switch phải từ 1 đến 64")
        delay = float(delay or 0.0)
        if delay < 0:
            raise ValueError("Độ trễ Switch phải >= 0")
        sw = {
            "name": name,
            "ports": ports,
            "delay": delay,
            "bandwidth": bandwidth if bandwidth not in ("", 0) else None,
            "queue_limit": int(queue_limit) if queue_limit not in (None, "", 0) else None,
            "x": float(x),
            "y": float(y),
        }
        self.switches[name] = sw
        return deepcopy(sw)

    def update_switch(
        self,
        old_name: str,
        name: str | None = None,
        ports: int | None = None,
        delay: float | None = None,
        bandwidth: int | str | None = ...,  # type: ignore[assignment]
        queue_limit: int | None = ...,      # type: ignore[assignment]
    ) -> dict[str, Any]:
        if old_name not in self.switches:
            raise ValueError(f"Switch không tồn tại: {old_name!r}")
        cur = self.switches[old_name]
        new_name = (name if name is not None else cur["name"]).strip()
        self._check_unique_name(new_name, ignore=old_name)
        new_ports = int(ports if ports is not None else cur["ports"])
        if new_ports < 1 or new_ports > 64:
            raise ValueError("Số cổng Switch phải từ 1 đến 64")

        used = self.used_ports(old_name)
        if used and max(used.keys()) > new_ports:
            raise ValueError(
                f"Cổng {max(used.keys())} đang có kết nối, không thể giảm số cổng xuống {new_ports}"
            )

        new_delay = float(delay if delay is not None else cur.get("delay", 0.0))
        if new_delay < 0:
            raise ValueError("Độ trễ Switch phải >= 0")
        new_bw = cur.get("bandwidth") if bandwidth is ... else (bandwidth if bandwidth not in ("", 0) else None)
        new_ql = cur.get("queue_limit") if queue_limit is ... else (int(queue_limit) if queue_limit not in (None, "", 0) else None)

        updated = {
            **cur,
            "name": new_name,
            "ports": new_ports,
            "delay": new_delay,
            "bandwidth": new_bw,
            "queue_limit": new_ql,
        }
        if new_name != old_name:
            del self.switches[old_name]
            self.switches[new_name] = updated
            for lnk in self.links:
                if lnk["a"] == old_name:
                    lnk["a"] = new_name
                if lnk["b"] == old_name:
                    lnk["b"] = new_name
            for cl in self.control_links:
                if cl["switch"] == old_name:
                    cl["switch"] = new_name
        else:
            self.switches[old_name] = updated
        return deepcopy(updated)

    def delete_switch(self, name: str) -> None:
        if name not in self.switches:
            raise ValueError(f"Switch không tồn tại: {name!r}")
        del self.switches[name]
        self.links = [l for l in self.links if l["a"] != name and l["b"] != name]
        self.control_links = [cl for cl in self.control_links if cl["switch"] != name]

    # --- CRUD Controller ---
    def add_controller(
        self,
        name: str | None = None,
        mode: str = "openflow",
        idle_timeout: float | None = None,
        host: str = "127.0.0.1",
        port: int = 6653,
        ports: int = 8,
        x: float = 380.0,
        y: float = 55.0,
    ) -> dict[str, Any]:
        name = (name or self.next_controller_name()).strip()
        self._check_unique_name(name)
        if mode != "openflow":
            raise ValueError("Controller chỉ hỗ trợ OpenFlow qua os-ken")
        ports = int(ports)
        if ports < 1 or ports > 64:
            raise ValueError("Số cổng Controller phải từ 1 đến 64")
        ctrl = {
            "name": name,
            "mode": mode,
            "idle_timeout": float(idle_timeout) if idle_timeout not in (None, "", 0) else None,
            "host": host.strip() or "127.0.0.1",
            "port": int(port),
            "ports": ports,
            "x": float(x),
            "y": float(y),
        }
        self.controllers[name] = ctrl
        return deepcopy(ctrl)

    def update_controller(
        self,
        old_name: str,
        name: str | None = None,
        mode: str | None = None,
        idle_timeout: float | None = ...,  # type: ignore[assignment]
        host: str | None = None,
        port: int | None = None,
        ports: int | None = None,
    ) -> dict[str, Any]:
        if old_name not in self.controllers:
            raise ValueError(f"Controller không tồn tại: {old_name!r}")
        cur = self.controllers[old_name]
        new_name = (name if name is not None else cur["name"]).strip()
        self._check_unique_name(new_name, ignore=old_name)
        new_ports = int(ports if ports is not None else cur["ports"])
        if new_ports < 1 or new_ports > 64:
            raise ValueError("Số cổng Controller phải từ 1 đến 64")

        used = self.used_ports(old_name)
        if used and max(used.keys()) > new_ports:
            raise ValueError(
                f"Cổng {max(used.keys())} của Controller đang nối Switch, không thể giảm xuống {new_ports}"
            )

        if mode is not None and mode != "openflow":
            raise ValueError("Controller chỉ hỗ trợ OpenFlow qua os-ken")

        new_idle = cur["idle_timeout"]
        if idle_timeout is not ...:
            new_idle = float(idle_timeout) if idle_timeout not in (None, "", 0) else None

        updated = {
            **cur,
            "name": new_name,
            "mode": "openflow",
            "idle_timeout": new_idle,
            "host": (host.strip() or "127.0.0.1") if host is not None else cur["host"],
            "port": int(port) if port is not None else cur["port"],
            "ports": new_ports,
        }
        if new_name != old_name:
            del self.controllers[old_name]
            self.controllers[new_name] = updated
            for cl in self.control_links:
                if cl["controller"] == old_name:
                    cl["controller"] = new_name
        else:
            self.controllers[old_name] = updated
        return deepcopy(updated)

    def delete_controller(self, name: str) -> None:
        if name not in self.controllers:
            raise ValueError(f"Controller không tồn tại: {name!r}")
        del self.controllers[name]
        self.control_links = [cl for cl in self.control_links if cl["controller"] != name]

    # --- CRUD Control Link (Controller <-> Switch) ---
    def add_control_link(self, controller_name: str, c_port: int, switch_name: str) -> dict[str, Any]:
        c_port = int(c_port)
        if controller_name not in self.controllers:
            raise ValueError(f"Controller không tồn tại: {controller_name!r}")
        if switch_name not in self.switches:
            raise ValueError(f"Switch không tồn tại: {switch_name!r}")

        max_cp = self.get_port_count(controller_name)
        if c_port < 1 or c_port > max_cp:
            raise ValueError(f"Cổng {c_port} của {controller_name!r} vượt quá giới hạn (1..{max_cp})")

        used_c = self.used_ports(controller_name)
        if c_port in used_c:
            raise ValueError(f"Cổng {c_port} của {controller_name!r} đã nối tới {used_c[c_port]}")

        existing = self.switch_controller(switch_name)
        if existing:
            raise ValueError(
                f"Switch {switch_name!r} đã nối tới Controller {existing['controller']}:{existing['c_port']}"
            )

        cl = {"controller": controller_name, "c_port": c_port, "switch": switch_name}
        self.control_links.append(cl)
        return deepcopy(cl)

    def delete_control_link(self, controller_name: str, c_port: int, switch_name: str) -> bool:
        c_port = int(c_port)
        before = len(self.control_links)
        self.control_links = [
            cl for cl in self.control_links
            if not (cl["controller"] == controller_name and cl["c_port"] == c_port and cl["switch"] == switch_name)
        ]
        return len(self.control_links) < before

    # --- CRUD Data Link (Host/Switch <-> Host/Switch) ---
    def add_link(
        self,
        a: str,
        a_port: int,
        b: str,
        b_port: int,
        delay: float = 0.001,
        bandwidth: int | str | None = None,
        loss: float = 0.0,
        queue_limit: int | None = None,
    ) -> dict[str, Any]:
        a_port, b_port = int(a_port), int(b_port)
        if a == b:
            raise ValueError("Không thể nối một thiết bị với chính nó")
        if a in self.controllers or b in self.controllers:
            raise ValueError("Controller chỉ có thể nối với Switch qua control_links")
        for node_name, port in ((a, a_port), (b, b_port)):
            max_p = self.get_port_count(node_name)
            if port < 1 or port > max_p:
                raise ValueError(f"Cổng {port} của {node_name!r} vượt quá giới hạn (1..{max_p})")
            if node_name in self.hosts and port != 1:
                raise ValueError(f"Host {node_name!r} chỉ hỗ trợ cổng 1")
            used = self.used_ports(node_name)
            if port in used:
                raise ValueError(f"Cổng {port} của {node_name!r} đã nối tới {used[port]}")

        lnk: dict[str, Any] = {
            "a": a,
            "a_port": a_port,
            "b": b,
            "b_port": b_port,
            "delay": float(delay),
            "bandwidth": bandwidth if bandwidth not in ("", 0) else None,
            "loss": float(loss or 0.0),
            "queue_limit": int(queue_limit) if queue_limit not in (None, "", 0) else None,
        }
        self.links.append(lnk)
        return deepcopy(lnk)

    def delete_link(self, a: str, a_port: int, b: str, b_port: int) -> bool:
        a_port, b_port = int(a_port), int(b_port)
        before = len(self.links)
        self.links = [
            l for l in self.links
            if not (
                (l["a"] == a and l["a_port"] == a_port and l["b"] == b and l["b_port"] == b_port)
                or (l["a"] == b and l["a_port"] == b_port and l["b"] == a and l["b_port"] == a_port)
            )
        ]
        return len(self.links) < before

    def update_positions(self, positions: dict[str, dict[str, float]]) -> None:
        for name, pos in positions.items():
            if name in self.controllers:
                self.controllers[name]["x"] = float(pos["x"])
                self.controllers[name]["y"] = float(pos["y"])
            elif name in self.hosts:
                self.hosts[name]["x"] = float(pos["x"])
                self.hosts[name]["y"] = float(pos["y"])
            elif name in self.switches:
                self.switches[name]["x"] = float(pos["x"])
                self.switches[name]["y"] = float(pos["y"])

    # --- Tuần tự hóa & kiểm tra bằng build_network ---
    def to_spec(self) -> dict[str, Any]:
        spec: dict[str, Any] = {
            "hosts": deepcopy(list(self.hosts.values())),
            "switches": deepcopy(list(self.switches.values())),
            "links": deepcopy(self.links),
            "controllers": deepcopy(list(self.controllers.values())),
            "control_links": deepcopy(self.control_links),
        }
        first_ctrl = next(iter(self.controllers.values()), None)
        if first_ctrl and first_ctrl.get("idle_timeout") is not None:
            spec["controller_idle_timeout"] = first_ctrl["idle_timeout"]
        if self.seed is not None:
            spec["seed"] = self.seed
        return spec

    def validate(self) -> None:
        scheduler = Scheduler()
        build_network(self.to_spec(), scheduler=scheduler, channel=Channel(scheduler))

    @classmethod
    def from_spec(cls, spec: dict[str, Any]) -> "TopologyModel":
        m = cls(with_default_controller=False)
        m.seed = spec.get("seed")

        raw_switches = spec.get("switches", [])
        raw_hosts = spec.get("hosts", [])
        raw_links = spec.get("links", [])

        max_port: dict[str, int] = {}
        for lnk in raw_links:
            max_port[lnk["a"]] = max(max_port.get(lnk["a"], 0), int(lnk["a_port"]))
            max_port[lnk["b"]] = max(max_port.get(lnk["b"], 0), int(lnk["b_port"]))

        for idx, s in enumerate(raw_switches):
            name = s["name"]
            m.switches[name] = {
                "name": name,
                "ports": int(s.get("ports", max(4, max_port.get(name, 4)))),
                "delay": float(s.get("delay", 0.0)),
                "bandwidth": s.get("bandwidth") if s.get("bandwidth") not in ("", 0) else None,
                "queue_limit": s.get("queue_limit"),
                "x": float(s.get("x", 200.0 + idx * 220.0)),
                "y": float(s.get("y", 175.0)),
            }

        attached: dict[str, list[str]] = {}
        for lnk in raw_links:
            for a, b in ((lnk["a"], lnk["b"]), (lnk["b"], lnk["a"])):
                if any(h["name"] == a for h in raw_hosts) and b in m.switches:
                    attached.setdefault(b, []).append(a)

        auto_host_pos: dict[str, tuple[float, float]] = {}
        for sw_name, hs in attached.items():
            sx = m.switches[sw_name]["x"]
            for j, h_name in enumerate(hs):
                auto_host_pos[h_name] = (sx + (j - (len(hs) - 1) / 2) * 120.0, 315.0)

        for idx, h in enumerate(raw_hosts):
            name = h["name"]
            dx, dy = auto_host_pos.get(name, (140.0 + idx * 130.0, 315.0))
            m.hosts[name] = {
                "name": name,
                "mac": h["mac"],
                "ip": h.get("ip", ""),
                "ports": 1,
                "x": float(h.get("x", dx)),
                "y": float(h.get("y", dy)),
            }

        for lnk in raw_links:
            m.add_link(
                lnk["a"],
                lnk["a_port"],
                lnk["b"],
                lnk["b_port"],
                delay=lnk.get("delay", 0.001),
                bandwidth=lnk.get("bandwidth"),
                loss=lnk.get("loss", 0.0),
                queue_limit=lnk.get("queue_limit"),
            )

        # Nạp controllers & control_links (hỗ trợ cả JSON mới lẫn JSON cũ)
        if "controllers" in spec:
            for idx, c in enumerate(spec["controllers"]):
                m.add_controller(
                    name=c["name"],
                    mode="openflow",
                    idle_timeout=c.get("idle_timeout"),
                    host=c.get("host", "127.0.0.1"),
                    port=c.get("port", 6653),
                    ports=c.get("ports", 8),
                    x=c.get("x", 380.0 + idx * 180.0),
                    y=c.get("y", 55.0),
                )
            for cl in spec.get("control_links", []):
                m.add_control_link(cl["controller"], cl["c_port"], cl["switch"])
        elif isinstance(spec.get("controller"), dict):
            c = spec["controller"]
            m.add_controller(
                name=c.get("name", "c0"),
                mode="openflow",
                idle_timeout=c.get("idle_timeout", spec.get("controller_idle_timeout")),
                host=c.get("host", "127.0.0.1"),
                port=c.get("port", 6653),
                ports=c.get("ports", max(8, len(m.switches))),
                x=c.get("x", 380.0),
                y=c.get("y", 55.0),
            )
            for idx, sw_name in enumerate(m.switches, start=1):
                m.add_control_link(c.get("name", "c0"), idx, sw_name)
        else:
            # JSON cũ như two_switch.json -> tự tạo c0 nối tới tất cả switch sẵn có
            cx = (
                sum(s["x"] for s in m.switches.values()) / len(m.switches)
                if m.switches
                else 380.0
            )
            m.add_controller(
                name="c0",
                mode="openflow",
                idle_timeout=spec.get("controller_idle_timeout"),
                ports=max(8, len(m.switches)),
                x=cx,
                y=55.0,
            )
            for idx, sw_name in enumerate(m.switches, start=1):
                m.add_control_link("c0", idx, sw_name)

        m.validate()
        return m