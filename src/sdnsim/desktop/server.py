from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from sdnsim.desktop.sim import SimulationManager
from sdnsim.topology.model import TopologyModel


def _find_ui_dir() -> Path:
    candidates: list[Path] = []
    # 1. Khi chạy bản đóng gói PyInstaller
    if getattr(sys, "frozen", False):
        meipass = Path(sys._MEIPASS)  # type: ignore[attr-defined]
        candidates.extend([
            meipass / "sdnsim" / "ui",
            meipass / "src" / "sdnsim" / "ui",
            meipass / "ui",
            Path(sys.executable).resolve().parent / "sdnsim" / "ui",
            Path(sys.executable).resolve().parent / "ui",
        ])
    # 2. Khi chạy từ mã nguồn
    here = Path(__file__).resolve().parent
    candidates.extend([
        here.parent / "ui",                          # src/sdnsim/ui
        here.parent.parent / "sdnsim" / "ui",
        here.parent.parent / "ui",
        Path.cwd() / "src" / "sdnsim" / "ui",
        Path.cwd() / "sdnsim" / "ui",
    ])
    for p in candidates:
        if (p / "templates").is_dir() or (p / "desktop.html").is_file():
            return p
    return here.parent / "ui"


UI_DIR = _find_ui_dir()


def _find_ui_file(name: str) -> Path:
    # Ưu tiên tìm trong templates/, nếu không có thì tìm ở thư mục ui/ gốc
    in_templates = UI_DIR / "templates" / name
    if in_templates.is_file():
        return in_templates
    in_root = UI_DIR / name
    if in_root.is_file():
        return in_root
    return in_templates


INDEX_HTML = _find_ui_file("index.html")
DESKTOP_HTML = _find_ui_file("desktop.html")


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
) -> FastAPI:
    if topologies_dir is not None:
        topo_dir = topologies_dir
    elif getattr(sys, "frozen", False):
        topo_dir = Path(sys.executable).resolve().parent / "topologies"
    else:
        repo_root = Path(__file__).resolve().parents[3]
        topo_dir = repo_root / "topologies"
    topo_dir.mkdir(parents=True, exist_ok=True)

    if initial_topology and initial_topology.exists():
        spec = json.loads(initial_topology.read_text(encoding="utf-8"))
        model = TopologyModel.from_spec(spec)
    else:
        model = TopologyModel(with_default_controller=False)

    sim = SimulationManager(model, topo_dir, step=step, speed=speed)
    reload_lock = asyncio.Lock()

    def _port_is_free(host: str, port: int) -> bool:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.1)
            return s.connect_ex((host, port)) != 0

    def _sync_osken_for_model() -> None:
        if not autorun:
            return
        has_of = any(c.get("mode") == "openflow" for c in sim.model.controllers.values())
        if not has_of:
            if sim.osken_running():
                sim.stop_osken()
            return
        for c in sim.model.controllers.values():
            if c.get("mode") == "openflow":
                c_host = c.get("host", "127.0.0.1")
                c_port = int(c.get("port", 6653))
                if c_host in ("127.0.0.1", "localhost") and not sim.osken_running() and _port_is_free(c_host, c_port):
                    try:
                        sim.start_osken(
                            port=c_port,
                            idle_timeout=c.get("idle_timeout"),
                            source_name=c.get("name", "c0"),
                        )
                    except Exception as exc:
                        sim.logs.push("controller", c.get("name", "c0"), "ERROR", f"Không thể tự bật os-ken: {exc}", sim.sched.now)
    
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
        sim.bind_loop(loop)
        task = asyncio.create_task(sim_loop()) if autorun else None

        if autorun:
            for c in sim.model.controllers.values():
                if c.get("mode") == "openflow":
                    sim.start_osken(
                        port=int(c.get("port", 6653)),
                        idle_timeout=c.get("idle_timeout"),
                        source_name=c.get("name", "c0"),
                    )
        try:
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

    # =========================================================================
    # MOUNT STATIC FILES ĐỂ TRÌNH DUYỆT TẢI ĐƯỢC CSS & JAVASCRIPT
    # =========================================================================
    scripts_dir = UI_DIR / "scripts"
    styles_dir = UI_DIR / "styles"
    templates_dir = UI_DIR / "templates"

    if scripts_dir.is_dir():
        app.mount("/scripts", StaticFiles(directory=scripts_dir), name="scripts")
        app.mount("/desktop/scripts", StaticFiles(directory=scripts_dir), name="desktop_scripts")

    if styles_dir.is_dir():
        app.mount("/styles", StaticFiles(directory=styles_dir), name="styles")
        app.mount("/desktop/styles", StaticFiles(directory=styles_dir), name="desktop_styles")

    if templates_dir.is_dir():
        app.mount("/templates", StaticFiles(directory=templates_dir), name="templates")
    # =========================================================================

    # --- Phục vụ giao diện HTML ---
    @app.get("/")
    def web_index():
        if not INDEX_HTML.is_file():
            raise HTTPException(500, f"Không tìm thấy file index.html tại: {INDEX_HTML}")
        return FileResponse(INDEX_HTML, headers={"Cache-Control": "no-store"})

    @app.get("/desktop")
    def desktop_index():
        if not DESKTOP_HTML.is_file():
            raise HTTPException(500, f"Không tìm thấy file desktop.html tại: {DESKTOP_HTML}")
        return FileResponse(DESKTOP_HTML, headers={"Cache-Control": "no-store"})

    @app.get("/favicon.ico")
    def favicon():
        for candidate in [
            UI_DIR / "templates" / "icon.png",
            UI_DIR / "icon.png",
            Path(__file__).resolve().parents[3] / "desktop" / "app.ico",
        ]:
            if candidate.is_file():
                return FileResponse(candidate)
        raise HTTPException(404, "Favicon không tồn tại")

    @app.get("/api/health")
    def health():
        return {"ok": True}

    # --- API Dashboard & Ping ---
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

    # --- API Quản lý Topology Studio ---
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
                _sync_osken_for_model()
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
                _sync_osken_for_model()
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

    # --- Controller Logging & Quản lý os-ken ---
    @app.get("/api/logs")
    def get_logs(since: int = 0, category: str | None = None, source: str | None = None):
        return {"entries": sim.logs.recent(since_id=since, category=category, source=source)}

    @app.post("/api/logs/clear")
    def clear_logs(category: str | None = None, source: str | None = None):
        sim.logs.clear(category=category, source=source)
        return {"ok": True}

    @app.post("/api/controller/osken/start")
    def start_osken(name: str | None = None):
        try:
            ctrl = None
            if name and name in sim.model.controllers:
                ctrl = sim.model.controllers[name]
            else:
                ctrl = next(
                    (c for c in sim.model.controllers.values() if c.get("mode") == "openflow"),
                    next(iter(sim.model.controllers.values()), None),
                )
            port = int(ctrl.get("port", 6653)) if ctrl else 6653
            idle = ctrl.get("idle_timeout") if ctrl else None
            src_name = ctrl.get("name", "c0") if ctrl else "c0"
            return sim.start_osken(port=port, idle_timeout=idle, source_name=src_name)
        except Exception as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/controller/osken/stop")
    def stop_osken():
        return sim.stop_osken()

    @app.websocket("/ws/logs")
    async def ws_logs(ws: WebSocket):
        await ws.accept()
        q = sim.logs.subscribe()
        try:
            for entry in sim.logs.recent(since_id=0):
                await ws.send_json(entry)
            while True:
                entry = await q.get()
                await ws.send_json(entry)
        except WebSocketDisconnect:
            pass
        finally:
            sim.logs.unsubscribe(q)

    # --- Bắt gói Wireshark (.pcap & Live TZSP) ---
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