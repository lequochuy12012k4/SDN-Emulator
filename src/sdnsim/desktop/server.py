from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from sdnsim.desktop.sim import SimulationManager
from sdnsim.topology.model import TopologyModel

UI_DIR = Path(__file__).resolve().parent.parent / "ui"
INDEX_HTML = UI_DIR / "template" / "index.html"
DESKTOP_HTML = UI_DIR / "template" / "desktop.html"


class PingRequest(BaseModel):
    src: str
    dst: str
    count: int = Field(default=1, ge=1, le=1000)
    interval: float = Field(default=0.2, ge=0.001, le=60.0)


class ExecRequest(BaseModel):
    command: str


class LoadFileRequest(BaseModel):
    path: str


class SaveFileRequest(BaseModel):
    name: str

class CaptureToggleRequest(BaseModel):
    enabled: bool

def create_desktop_app(
    initial_topology: Path | None = None,
    topologies_dir: Path | None = None,
    step: float = 0.05,
    speed: float = 1.0,
    autorun: bool = True,
    push_interval: float = 0.2,
    autostart_osken: bool = True,
) -> FastAPI:
    repo_root = Path(__file__).resolve().parents[3]
    topo_dir = topologies_dir or (repo_root / "topologies")
    topo_dir.mkdir(parents=True, exist_ok=True)

    if initial_topology and initial_topology.exists():
        spec = json.loads(initial_topology.read_text(encoding="utf-8"))
        model = TopologyModel.from_spec(spec)
    else:
        model = TopologyModel(with_default_controller=False)

    sim = SimulationManager(model, topo_dir, step=step, speed=speed)
    reload_lock = asyncio.Lock()

    async def sim_loop() -> None:
        while True:
            try:
                sim.sched.run(until=sim.sched.now + sim.step)
            except Exception as exc:
                sim.logs.push("controller", "sim", "ERROR", f"Lỗi sim_loop: {exc}", sim.sched.now)
            await asyncio.sleep(sim.step / sim.speed)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        loop = asyncio.get_running_loop()
        task = None
        try:
            ctrl = sim.local_osken_controller()
            if autostart_osken and ctrl:
                sim.start_osken(
                    port=int(ctrl["port"]),
                    idle_timeout=ctrl.get("idle_timeout"),
                    source_name=ctrl["name"],
                )
            sim.bind_loop(loop)
            task = asyncio.create_task(sim_loop()) if autorun else None
            yield
        finally:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            await sim._stop_openflow_channels()
            sim.shutdown_sync()

    app = FastAPI(title="SDN Emulator Desktop + Web", lifespan=lifespan)
    app.state.sim = sim
    app.state.sched = sim.sched
    app.mount("/assets", StaticFiles(directory=UI_DIR), name="assets")

    # --- Trang giao diện ---
    @app.get("/")
    def web_index():
        return FileResponse(INDEX_HTML, headers={"Cache-Control": "no-store"})

    @app.get("/desktop")
    def desktop_index():
        return FileResponse(DESKTOP_HTML, headers={"Cache-Control": "no-store"})

    @app.get("/api/health")
    def health():
        return {"ok": True}

    # --- API Dashboard Web tương thích ngược ---
    @app.get("/snapshot")
    def snapshot():
        return sim.snapshot()

    @app.post("/ping")
    def ping(req: PingRequest):
        try:
            seqs = sim.schedule_ping(req.src, req.dst, count=req.count, interval=req.interval)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"seq": seqs[-1], "seqs": seqs}

    @app.websocket("/ws")
    async def ws_snapshot(ws: WebSocket):
        await ws.accept()
        try:
            while True:
                await ws.send_json(sim.snapshot())
                try:
                    await asyncio.wait_for(ws.receive_text(), timeout=push_interval)
                except asyncio.TimeoutError:
                    pass
        except WebSocketDisconnect:
            pass

    # --- API Topology Studio (đồng bộ 100% Desktop <-> Web) ---
    @app.get("/api/topology")
    def get_topology():
        return {
            "spec": sim.model.to_spec(),
            "osken_running": sim.osken_running(),
        }

    @app.put("/api/topology")
    async def put_topology(spec: dict[str, Any]):
        async with reload_lock:
            try:
                new_model = TopologyModel.from_spec(spec)
                await sim.reload_topology(new_model)
                app.state.sched = sim.sched
            except Exception as exc:
                raise HTTPException(400, str(exc)) from exc
        return {"ok": True, "spec": sim.model.to_spec(), "snapshot": sim.snapshot()}

    @app.patch("/api/topology/positions")
    def patch_positions(positions: dict[str, dict[str, float]]):
        sim.model.update_positions(positions)
        return {"ok": True}

    @app.get("/api/topologies")
    def list_topologies():
        files = sorted(p.name for p in topo_dir.glob("*.json"))
        return {"files": files, "dir": str(topo_dir)}

    @app.post("/api/topology/load")
    async def load_topology_file(req: LoadFileRequest):
        p = Path(req.path)
        if not p.is_absolute():
            p = topo_dir / p
        if not p.exists():
            raise HTTPException(404, f"Không tìm thấy file: {p}")
        async with reload_lock:
            try:
                spec = json.loads(p.read_text(encoding="utf-8"))
                new_model = TopologyModel.from_spec(spec)
                await sim.reload_topology(new_model)
                app.state.sched = sim.sched
            except Exception as exc:
                raise HTTPException(400, str(exc)) from exc
        return {"ok": True, "spec": sim.model.to_spec()}

    @app.post("/api/topology/save")
    def save_topology_file(req: SaveFileRequest):
        name = req.name.strip()
        if not name:
            raise HTTPException(400, "Tên file không được trống")
        if not name.endswith(".json"):
            name += ".json"
        p = Path(name)
        if not p.is_absolute():
            p = topo_dir / p.name
        p.write_text(json.dumps(sim.model.to_spec(), indent=2, ensure_ascii=False), encoding="utf-8")
        return {"ok": True, "path": str(p)}

    # --- Host Console ---
    @app.post("/api/host/{host_name}/exec")
    def exec_host(host_name: str, req: ExecRequest):
        try:
            res = sim.exec_host_command(host_name, req.command)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        return res

    # --- Controller Logging & os-ken subprocess ---
    @app.get("/api/logs")
    def get_logs(since: int = 0, category: str | None = None, source: str | None = None):
        return {"entries": sim.logs.recent(since_id=since, category=category, source=source)}

    @app.post("/api/logs/clear")
    def clear_logs(category: str | None = None, source: str | None = None):
        sim.logs.clear(category=category, source=source)
        return {"ok": True}

    @app.post("/api/controller/osken/start")
    async def start_osken(name: str | None = None):
        try:
            ctrl = sim.local_osken_controller(name)
            if ctrl is None:
                raise ValueError("Không tìm thấy Controller os-ken cục bộ")
            if not sim.osken_running():
                await sim._stop_openflow_channels()
            result = sim.start_osken(
                port=int(ctrl["port"]),
                idle_timeout=ctrl.get("idle_timeout"),
                source_name=ctrl["name"],
            )
            sim._start_openflow_tasks()
            return result
        except Exception as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/controller/osken/stop")
    async def stop_osken():
        return sim.stop_osken()

    @app.websocket("/ws/logs")
    async def ws_logs(ws: WebSocket):
        await ws.accept()
        q = sim.logs.subscribe()
        try:
            # Gửi các log hiện có trước
            for entry in sim.logs.recent(since_id=0):
                await ws.send_json(entry)
            while True:
                entry = await q.get()
                await ws.send_json(entry)
        except WebSocketDisconnect:
            pass
        finally:
            sim.logs.unsubscribe(q)
    # --- Wireshark Capture (.pcap & Live TZSP) ---
    @app.get("/api/capture/status")
    def capture_status():
        return {
            "live_tzsp": sim.capture.live_tzsp,
            "tzsp_port": sim.capture.tzsp_port,
            "count": sim.capture.count(),
        }

    @app.post("/api/capture/tzsp")
    def capture_toggle_tzsp(req: CaptureToggleRequest):
        enabled = sim.capture.set_live(req.enabled)
        return {"live_tzsp": enabled, "tzsp_port": sim.capture.tzsp_port}

    @app.post("/api/capture/clear")
    def capture_clear():
        sim.capture.clear()
        return {"ok": True, "count": 0}

    @app.get("/api/capture/pcap")
    def capture_download_pcap():
        data = sim.capture.to_pcap_bytes()
        return Response(
            content=data,
            media_type="application/vnd.tcpdump.pcap",
            headers={"Content-Disposition": 'attachment; filename="sdnsim_capture.pcap"'},
        )
    return app