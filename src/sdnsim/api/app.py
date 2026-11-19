import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from sdnsim.core.host import Host
from sdnsim.core.network import load_network_from_json
from sdnsim.metrics.collector import Metrics
from pathlib import Path
from fastapi import WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

INDEX = Path(__file__).resolve().parent.parent / "ui" / "index.html"

class PingRequest(BaseModel):
    src: str
    dst: str


def create_app(topology_path, step=0.05, speed=1.0, autorun=True, push_interval=0.2):
    sched, ch, nodes = load_network_from_json(topology_path)
    metrics = Metrics(sched, ch, nodes)
    counter = {"seq": 0}

    async def sim_loop():
        while True:
            sched.run(until=sched.now + step)
            await asyncio.sleep(step / speed)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(sim_loop()) if autorun else None
        yield
        if task:
            task.cancel()

    app = FastAPI(lifespan=lifespan)
    app.state.sched = sched

    @app.get("/")
    async def index():
        return FileResponse(INDEX)

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        try:
            while True:
                await ws.send_json(metrics.snapshot())
                try:   # chờ tối đa push_interval; đồng thời phát hiện client ngắt kết nối
                    await asyncio.wait_for(ws.receive_text(), timeout=push_interval)
                except asyncio.TimeoutError:
                    pass
        except WebSocketDisconnect:
            pass

    @app.get("/snapshot")
    def snapshot():
        return metrics.snapshot()

    @app.post("/ping")
    def ping(req: PingRequest):
        src, dst = nodes.get(req.src), nodes.get(req.dst)
        if not isinstance(src, Host) or not isinstance(dst, Host):
            raise HTTPException(404, "src và dst phải là host có trong mạng")
        counter["seq"] += 1
        sched.schedule(0, src.ping_ip, dst.ip, counter["seq"])
        return {"seq": counter["seq"]}

    return app