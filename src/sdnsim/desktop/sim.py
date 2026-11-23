from __future__ import annotations

import asyncio
from collections import Counter, deque
from datetime import datetime
import inspect
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import threading
import time
from typing import Any

from sdnsim.core.host import Host
from sdnsim.core.scheduler import Scheduler
from sdnsim.core.switch import Switch
from sdnsim.metrics.collector import Metrics
from sdnsim.openflow.channel import OpenFlowChannel
from sdnsim.topology.model import TopologyModel
from sdnsim.topology.network import build_network
from sdnsim.desktop.capture import PacketCapture

class LogHub:
    """Kho lưu log vòng tròn cho Controller Console và Host Console."""

    def __init__(self, maxlen: int = 1000) -> None:
        self._entries: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._seq = 0
        self._lock = threading.Lock()
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def push(
        self,
        category: str,
        source: str,
        level: str,
        message: str,
        sim_time: float | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            entry = {
                "id": self._seq,
                "ts": datetime.now().strftime("%H:%M:%S.%f")[:-3],
                "sim_time": round(sim_time, 4) if sim_time is not None else None,
                "category": category,
                "source": source,
                "level": level,
                "message": message,
            }
            self._entries.append(entry)
            subs = list(self._subscribers)
            loop = self._loop

        if loop and subs and not loop.is_closed():
            for q in subs:
                loop.call_soon_threadsafe(self._safe_put, q, entry)
        return entry

    @staticmethod
    def _safe_put(q: asyncio.Queue[dict[str, Any]], entry: dict[str, Any]) -> None:
        if q.full():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            q.put_nowait(entry)
        except asyncio.QueueFull:
            pass

    def recent(
        self,
        since_id: int = 0,
        category: str | None = None,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._entries)
        out = []
        for e in items:
            if e["id"] <= since_id:
                continue
            if category and e["category"] != category:
                continue
            if source and e["source"] != source:
                continue
            out.append(e)
        return out

    def clear(self, category: str | None = None, source: str | None = None) -> None:
        with self._lock:
            if category is None and source is None:
                self._entries.clear()
                return
            kept = [
                e for e in self._entries
                if not (
                    (category is None or e["category"] == category)
                    and (source is None or e["source"] == source)
                )
            ]
            self._entries = deque(kept, maxlen=self._entries.maxlen)

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=300)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(q)


def _create_openflow_channel(sched: Any, host: str, port: int) -> Any:
    sig = inspect.signature(OpenFlowChannel.__init__)
    params = [p for p in sig.parameters if p != "self"]
    if len(params) >= 3 and params[1] in ("host", "controller_host"):
        return OpenFlowChannel(sched, host, port)
    if len(params) >= 2 and params[1] in ("addr", "address", "target"):
        return OpenFlowChannel(sched, (host, port))
    try:
        return OpenFlowChannel(sched, host, port)
    except TypeError:
        return OpenFlowChannel(sched, f"{host}:{port}")


class ControlPlaneHub:
    """Kênh điều khiển trung tâm ánh xạ đúng theo control_links (Controller <-> Switch)."""

    def __init__(
        self,
        sched: Scheduler,
        model: TopologyModel,
        logs: LogHub,
        capture: PacketCapture | None = None,
        down_latency: float = 0.0,
    ) -> None:
        self.sched = sched
        self.model = model
        self.logs = logs
        self.capture = capture
        self.down_latency = down_latency
        self.switches: dict[str, Switch] = {}
        self.packet_in_count = 0
        self.packet_in_by_switch: Counter[str] = Counter()

        self.switch_to_ctrl: dict[str, str] = {
            cl["switch"]: cl["controller"] for cl in model.control_links
        }
        self.of_channels: dict[str, OpenFlowChannel] = {}

        for c_name, c_cfg in model.controllers.items():
            self.of_channels[c_name] = _create_openflow_channel(
                sched, c_cfg["host"], int(c_cfg["port"])
            )

    def register(self, sw: Switch) -> None:
        self.switches[sw.name] = sw
        c_name = self.switch_to_ctrl.get(sw.name)
        if c_name and c_name in self.of_channels:
            self.of_channels[c_name].register(sw)

    def connected(self, sw_name: str) -> bool:  # noqa: F811
        c_name = self.switch_to_ctrl.get(sw_name)
        if not c_name:
            return False
        of_channel = self.of_channels.get(c_name)
        if of_channel is None:
            return False
        fn = getattr(of_channel, "connected", None)
        return bool(fn(sw_name)) if callable(fn) else False

    def packet_in(self, sw: Switch, in_port: int, pkt: Any) -> None:
        self.packet_in_count += 1
        self.packet_in_by_switch[sw.name] += 1

        c_name = self.switch_to_ctrl.get(sw.name)
        if not c_name:
            self.logs.push(
                "controller",
                sw.name,
                "ERROR",
                f"{sw.name}: Chưa nối Controller — gói PACKET_IN port={in_port} bị bỏ qua",
                self.sched.now,
            )
            return
        if self.capture:
            self.capture.record_packet_in(self.sched.now, sw.name, in_port, pkt)
        kind = getattr(pkt, "kind", "pkt")
        self.logs.push(
            "controller",
            c_name,
            "PACKET_IN",
            f"[{c_name}] {sw.name}: PACKET_IN port={in_port} {pkt.eth_src} -> {pkt.eth_dst} ({kind})",
            self.sched.now,
        )

        of_channel = self.of_channels.get(c_name)
        if of_channel is None:
            self.logs.push(
                "controller",
                sw.name,
                "ERROR",
                f"{sw.name}: Controller {c_name!r} không hỗ trợ OpenFlow",
                self.sched.now,
            )
            return
        of_channel.packet_in(sw, in_port, pkt)

    def send(self, msg: Any) -> None:
        sw = self.switches[msg.switch]
        if self.down_latency:
            self.sched.schedule(self.down_latency, sw.handle, msg)
        else:
            sw.handle(msg)


class SimulationManager:
    """Quản lý vòng đời simulator, đồng bộ topology và log cho Desktop + Web."""

    def __init__(
        self,
        initial_model: TopologyModel,
        topologies_dir: Path,
        step: float = 0.05,
        speed: float = 1.0,
    ) -> None:
        self.model = initial_model
        self.topologies_dir = topologies_dir
        self.step = step
        self.speed = speed
        self.logs = LogHub()
        self.seq_counter = 0
        self.sched: Any = None
        self.channel: ControlPlaneHub | None = None
        self.nodes: dict[str, Any] = {}
        self.metrics: Metrics | None = None
        self._of_tasks: list[asyncio.Task[Any]] = []
        self._osken_proc: subprocess.Popen[str] | None = None
        self._osken_port: int | None = None
        self._osken_idle_timeout: float | None = None
        self._osken_source_name: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.capture = PacketCapture()
        self._build_sync(self.model)

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self.logs.bind_loop(loop)
        self._start_openflow_tasks()

    def _start_openflow_tasks(self) -> None:
        for t in self._of_tasks:
            if not t.done():
                t.cancel()
        self._of_tasks = []
        if not self._loop or not self.channel:
            return
        for of_ch in self.channel.of_channels.values():
            start_fn = getattr(of_ch, "start", None) or getattr(of_ch, "run", None)
            if callable(start_fn):
                coro = start_fn()
                if inspect.isawaitable(coro):
                    self._of_tasks.append(self._loop.create_task(coro))

    async def _stop_openflow_channels(self) -> None:
        for t in self._of_tasks:
            if not t.done():
                t.cancel()
                try:
                    await t
                except BaseException:
                    pass
        self._of_tasks = []
        if self.channel:
            for of_ch in self.channel.of_channels.values():
                for m_name in ("stop", "close"):
                    fn = getattr(of_ch, m_name, None)
                    if callable(fn):
                        res = fn()
                        if inspect.isawaitable(res):
                            await res
                        break

    def _attach_log_hooks(self) -> None:
        sched = self.sched
        assert self.channel is not None
        sw_to_ctrl = self.channel.switch_to_ctrl
        seen_links: set[int] = set()
        for node in self.nodes.values():
            for link in node.ports.values():
                if id(link) in seen_links:
                    continue
                seen_links.add(id(link))
                orig_transmit = link.transmit

                def make_transmit(fn: Any) -> Any:
                    def wrapped_transmit(sender: Any, pkt: Any) -> Any:
                        self.capture.record(sched.now, pkt)
                        return fn(sender, pkt)
                    return wrapped_transmit
                link.transmit = make_transmit(orig_transmit)
        for node in self.nodes.values():
            if isinstance(node, Switch):
                orig_flow_mod = node.flow_mod

                def make_flow_mod(sw_obj: Switch, fn: Any) -> Any:
                    def wrapped_flow_mod(
                        match: dict,
                        out_port: Any,
                        priority: int = 1,
                        idle_timeout: float | None = None,
                        cookie: int = 0,
                        group_id: int | None = None,
                        meter_id: int | None = None,
                    ) -> Any:
                        c_name = sw_to_ctrl.get(sw_obj.name, "ctrl")
                        self.capture.record_flow_mod(
                            sched.now, sw_obj.name, match, out_port, priority, idle_timeout
                        )
                        self.logs.push(
                            "controller",
                            c_name,
                            "FLOW_MOD",
                            f"[{c_name}] {sw_obj.name}: FLOW_MOD match={match} -> port {out_port} (prio={priority}, idle_timeout={idle_timeout})",
                            sched.now,
                        )
                        return fn(match, out_port, priority, idle_timeout, cookie, group_id, meter_id)
                    return wrapped_flow_mod

                node.flow_mod = make_flow_mod(node, orig_flow_mod)  # type: ignore[method-assign]

                orig_packet_out = node.packet_out

                def make_packet_out(sw_obj: Switch, fn: Any) -> Any:
                    def wrapped_packet_out(pkt: Any, out_port: Any, in_port: int | None = None) -> Any:
                        c_name = sw_to_ctrl.get(sw_obj.name, "ctrl")
                        self.capture.record_packet_out(
                            sched.now, sw_obj.name, pkt, out_port, in_port
                        )
                        self.logs.push(
                            "controller",
                            c_name,
                            "PACKET_OUT",
                            f"[{c_name}] {sw_obj.name}: PACKET_OUT -> port {out_port} (in_port={in_port})",
                            sched.now,
                        )
                        return fn(pkt, out_port, in_port)
                    return wrapped_packet_out

                node.packet_out = make_packet_out(node, orig_packet_out)  # type: ignore[method-assign]

                if hasattr(node, "_sweep"):
                    orig_sweep = node._sweep

                    def make_sweep(sw_obj: Switch, fn: Any) -> Any:
                        def wrapped_sweep() -> Any:
                            before = len(sw_obj.flows)
                            res = fn()
                            after = len(sw_obj.flows)
                            if after < before:
                                c_name = sw_to_ctrl.get(sw_obj.name, "ctrl")
                                self.logs.push(
                                    "controller",
                                    c_name,
                                    "EXPIRED",
                                    f"[{c_name}] {sw_obj.name}: {before - after} flow hết hạn",
                                    sched.now,
                                )
                            return res
                        return wrapped_sweep

                    node._sweep = make_sweep(node, orig_sweep)  # type: ignore[method-assign]

            elif isinstance(node, Host):
                orig_receive = node.receive

                def make_host_receive(h_obj: Host, fn: Any) -> Any:
                    def wrapped_receive(port: int, pkt: Any) -> Any:
                        before_len = len(h_obj.rtts)
                        res = fn(port, pkt)
                        if len(h_obj.rtts) > before_len:
                            rtt_ms = h_obj.rtts[-1] * 1000.0
                            src_ip = getattr(pkt, "ip_src", None) or getattr(pkt, "eth_src", "?")
                            self.logs.push(
                                "host",
                                h_obj.name,
                                "ICMP",
                                f"[{sched.now*1000:7.2f} ms] reply from {src_ip} seq={pkt.seq} rtt={rtt_ms:.2f} ms",
                                sched.now,
                            )
                        return res
                    return wrapped_receive

                node.receive = make_host_receive(node, orig_receive)  # type: ignore[method-assign]

    def _build_sync(self, model: TopologyModel) -> None:
        model.validate()
        spec = model.to_spec()
        sched = Scheduler()
        hub = ControlPlaneHub(sched, model, self.logs, capture=self.capture)
        sched, ch, nodes = build_network(spec, scheduler=sched, channel=hub, controller=None)

        self.model = model
        self.sched = sched
        self.channel = hub
        self.nodes = nodes
        self.metrics = Metrics(sched, hub, nodes)
        self._attach_log_hooks()

        self.logs.push(
            "controller",
            "system",
            "INFO",
            f"Đã đồng bộ mạng: {len(model.controllers)} controller, {len(model.switches)} switch, {len(model.hosts)} host, {len(model.links)} data link, {len(model.control_links)} control link",
            sched.now,
        )

    async def reload_topology(self, new_model: TopologyModel) -> None:
        new_model.validate()
        was_running = self.osken_running()
        new_ctrl = self.local_osken_controller(model=new_model)
        config_changed = was_running and (
            new_ctrl is None
            or int(new_ctrl["port"]) != self._osken_port
            or new_ctrl.get("idle_timeout") != self._osken_idle_timeout
            or new_ctrl["name"] != self._osken_source_name
        )
        await self._stop_openflow_channels()
        if config_changed:
            self.stop_osken()
        self._build_sync(new_model)
        if was_running and config_changed and new_ctrl is not None:
            self.start_osken(
                port=int(new_ctrl["port"]),
                idle_timeout=new_ctrl.get("idle_timeout"),
                source_name=new_ctrl["name"],
            )
        self._start_openflow_tasks()

    def snapshot(self) -> dict[str, Any]:
        assert self.metrics is not None
        snap = self.metrics.snapshot()
        for name, sw_spec in self.model.switches.items():
            sw_snap = snap["switches"].get(name)
            sw_obj = self.nodes.get(name)
            if sw_snap is not None:
                sw_snap["delay"] = getattr(sw_obj, "delay", sw_spec.get("delay", 0.0))
                sw_snap["bandwidth"] = getattr(sw_obj, "bandwidth", None) or sw_spec.get("bandwidth")
                sw_snap["dropped_packets"] = getattr(sw_obj, "dropped_packets", 0)

        snap["layout"] = {
            "controllers": list(self.model.controllers.values()),
            "control_links": list(self.model.control_links),
            "hosts": {
                name: {"x": h["x"], "y": h["y"], "ports": h["ports"]}
                for name, h in self.model.hosts.items()
            },
            "switches": {
                name: {
                    "x": s["x"],
                    "y": s["y"],
                    "ports": s["ports"],
                    "delay": s.get("delay", 0.0),
                    "bandwidth": s.get("bandwidth"),
                }
                for name, s in self.model.switches.items()
            },
        }
        return snap

    def schedule_ping(self, src_name: str, dst_name: str, count: int = 1, interval: float = 0.2) -> list[int]:
        src = self.nodes.get(src_name)
        dst = self.nodes.get(dst_name)
        if not isinstance(src, Host) or not isinstance(dst, Host):
            raise ValueError("src và dst phải là host có trong mạng")
        if 1 not in src.ports:
            raise ValueError(f"Host {src_name} chưa được nối link vào cổng 1")
        if not dst.ip:
            raise ValueError(f"Host {dst_name} chưa có địa chỉ IP")

        seqs: list[int] = []
        for idx in range(count):
            self.seq_counter += 1
            seqs.append(self.seq_counter)
            self.sched.schedule(idx * interval, src.ping_ip, dst.ip, self.seq_counter)
        return seqs

    def exec_host_command(self, host_name: str, command: str) -> dict[str, Any]:
        host = self.nodes.get(host_name)
        if not isinstance(host, Host):
            raise ValueError(f"Không tìm thấy host {host_name!r}")

        tokens = shlex.split(command.strip())
        if not tokens:
            return {"ok": True, "lines": [], "clear": False}

        cmd = tokens[0].lower()
        if cmd == "clear":
            self.logs.clear(category="host", source=host_name)
            return {"ok": True, "lines": [], "clear": True}

        if cmd == "help":
            return {
                "ok": True,
                "lines": [
                    "Các lệnh hỗ trợ trên Host Console:",
                    "  ping <host|ip> [-c N] [-i sec]  - Gửi ICMP ping tới host khác",
                    "  pingall                         - Ping tới toàn bộ host còn lại",
                    "  arp                             - Xem bảng ARP của host",
                    "  ifconfig                        - Xem cấu hình IP, MAC và trạng thái cổng",
                    "  clear                           - Xóa màn hình console",
                ],
            }

        if cmd == "ifconfig":
            used = self.model.used_ports(host_name)
            link_info = f"connected -> {used[1]}" if 1 in used else "disconnected"
            return {
                "ok": True,
                "lines": [
                    f"{host.name}-eth1: MAC={host.mac}  IP={host.ip or '(none)'}  [{link_info}]",
                    f"  stats: rtts={len(host.rtts)}, arp_failures={getattr(host, 'arp_failures', 0)}",
                ],
            }

        if cmd == "arp":
            if not host.arp_table:
                return {"ok": True, "lines": ["Bảng ARP rỗng."]}
            lines = ["IP Address       HW Address"]
            for ip, mac in host.arp_table.items():
                lines.append(f"{ip:<16} {mac}")
            return {"ok": True, "lines": lines}

        if cmd == "pingall":
            others = [h for h in self.model.hosts if h != host_name]
            if not others:
                return {"ok": False, "lines": ["Không có host nào khác trong topology."]}
            if 1 not in host.ports:
                return {"ok": False, "lines": [f"Lỗi: {host_name} chưa nối link (cổng 1 trống)."]}
            out_lines = []
            for idx, target_name in enumerate(others):
                target = self.nodes[target_name]
                self.seq_counter += 1
                self.sched.schedule(idx * 0.15, host.ping_ip, target.ip, self.seq_counter)
                out_lines.append(f"PING {target_name} ({target.ip}) seq={self.seq_counter}...")
            return {"ok": True, "lines": out_lines}

        if cmd == "ping":
            if len(tokens) < 2:
                return {"ok": False, "lines": ["Cú pháp: ping <tên_host|ip> [-c số_lần] [-i khoảng_giây]"]}
            target_arg = tokens[1]
            count = 1
            interval = 0.25
            i = 2
            while i < len(tokens):
                if tokens[i] == "-c" and i + 1 < len(tokens):
                    count = max(1, min(100, int(tokens[i + 1])))
                    i += 2
                elif tokens[i] == "-i" and i + 1 < len(tokens):
                    interval = max(0.01, float(tokens[i + 1]))
                    i += 2
                else:
                    return {"ok": False, "lines": [f"Tham số không hợp lệ: {tokens[i]}"]}

            dst_name = None
            if target_arg in self.model.hosts:
                dst_name = target_arg
            else:
                for h_name, h_data in self.model.hosts.items():
                    if h_data.get("ip") == target_arg:
                        dst_name = h_name
                        break
            if not dst_name:
                return {"ok": False, "lines": [f"Không tìm thấy host hoặc IP {target_arg!r}."]}
            if dst_name == host_name:
                return {"ok": False, "lines": ["Không thể tự ping chính mình."]}
            try:
                seqs = self.schedule_ping(host_name, dst_name, count=count, interval=interval)
            except ValueError as exc:
                return {"ok": False, "lines": [f"Lỗi: {exc}"]}

            dst_ip = self.model.hosts[dst_name]["ip"]
            return {
                "ok": True,
                "lines": [
                    f"PING {dst_name} ({dst_ip}) từ {host_name} ({host.ip}): {count} gói (seq={seqs[0]}..{seqs[-1]})"
                ],
            }

        return {"ok": False, "lines": [f"Lệnh không hỗ trợ: {cmd!r}. Gõ 'help' để xem danh sách lệnh."]}

    def osken_running(self) -> bool:
        return self._osken_proc is not None and self._osken_proc.poll() is None

    def local_osken_controller(
        self,
        name: str | None = None,
        model: TopologyModel | None = None,
    ) -> dict[str, Any] | None:
        controllers = (model or self.model).controllers
        if name is not None:
            ctrl = controllers.get(name)
            if ctrl and ctrl["host"] in ("127.0.0.1", "localhost"):
                return ctrl
            return None
        return next(
            (ctrl for ctrl in controllers.values() if ctrl["host"] in ("127.0.0.1", "localhost")),
            None,
        )

    def start_osken(self, port: int = 6653, idle_timeout: float | None = None, source_name: str = "c0") -> dict[str, Any]:
        if self.osken_running():
            if self._osken_port == port and self._osken_idle_timeout == idle_timeout:
                return {"running": True, "pid": self._osken_proc.pid}  # type: ignore[union-attr]
            self.stop_osken()

        repo_root = Path(__file__).resolve().parents[3]
        script = repo_root / "examples" / "os_ken_controller.py"
        if not script.exists():
            raise FileNotFoundError(f"Không tìm thấy {script}")

        args = [sys.executable, "-u", str(script), str(port)]
        if idle_timeout is not None and int(idle_timeout) > 0:
            args.append(str(int(idle_timeout)))

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        self._osken_proc = subprocess.Popen(
            args,
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        proc = self._osken_proc
        self._osken_port = port
        self._osken_idle_timeout = idle_timeout
        self._osken_source_name = source_name

        def pump_output(process: subprocess.Popen[str]) -> None:
            assert process.stdout is not None
            for line in process.stdout:
                txt = line.rstrip()
                if txt:
                    self.logs.push("controller", source_name, "OPENFLOW", f"[os-ken] {txt}", self.sched.now)
            code = process.poll()
            self.logs.push("controller", source_name, "INFO", f"os_ken_controller đã thoát (code={code})", self.sched.now)

        threading.Thread(target=pump_output, args=(proc,), daemon=True).start()

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            exit_code = proc.poll()
            if exit_code is not None:
                self._osken_proc = None
                self._osken_port = None
                self._osken_idle_timeout = None
                self._osken_source_name = None
                raise RuntimeError(f"os_ken_controller đã thoát khi khởi động (code={exit_code})")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            self.stop_osken()
            raise TimeoutError(f"os_ken_controller không mở cổng 127.0.0.1:{port}")

        self.logs.push(
            "controller",
            source_name,
            "OPENFLOW",
            f"[{source_name}] Đã khởi động os_ken_controller.py (PID={proc.pid}) trên 127.0.0.1:{port}",
            self.sched.now,
        )
        return {"running": True, "pid": proc.pid}

    def stop_osken(self) -> dict[str, Any]:
        proc = self._osken_proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
            self.logs.push("controller", "system", "INFO", "Đã dừng os_ken_controller.py", self.sched.now)
        self._osken_proc = None
        self._osken_port = None
        self._osken_idle_timeout = None
        self._osken_source_name = None
        return {"running": False}

    def shutdown_sync(self) -> None:
        self.stop_osken()
        self.capture.close()