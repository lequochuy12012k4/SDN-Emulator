import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from sdnsim.core.host import Host
from sdnsim.core.scheduler import Scheduler
from sdnsim.metrics.collector import Metrics
from sdnsim.openflow.channel import OpenFlowChannel
from sdnsim.topology.network import build_network

INDEX = Path(__file__).resolve().parent.parent / "ui" / "index.html"
log = logging.getLogger("sdnsim")


class PingRequest(BaseModel):
    src: str
    dst: str
    count: int = Field(default=1, ge=1, le=1000)          # số ping gửi
    interval: float = Field(default=1.0, gt=0, le=60)     # giây (giờ ảo) giữa hai ping liên tiếp


def create_app(topology_path, step=0.05, speed=1.0, autorun=True, push_interval=0.2,
               openflow=None, down_latency=0.001, retry_interval=2.0):
    """Dựng ứng dụng web điều khiển và quan sát một mạng mô phỏng.

    openflow: None để dùng controller học MAC nội bộ, hoặc (host, port) để mỗi switch
        kết nối tới một controller OpenFlow 1.3 bên ngoài. Nếu controller chưa sẵn sàng,
        ứng dụng thử lại mỗi `retry_interval` giây; trong lúc đó PACKET_IN bị bỏ.
    """
    spec = json.loads(Path(topology_path).read_text(encoding="utf-8"))
    sched = Scheduler()
    channel = None
    if openflow is not None:
        host, port = openflow
        channel = OpenFlowChannel(sched, host, port, down_latency)
    sched, ch, nodes = build_network(spec, scheduler=sched, channel=channel)
    metrics = Metrics(sched, ch, nodes)
    counter = {"seq": 0}

    async def sim_loop():
        while True:
            try:
                sched.run(until=sched.now + step)
            except Exception:                       # một sự kiện lỗi không được làm dừng cả mô phỏng
                log.exception("lỗi khi chạy mô phỏng")
            await asyncio.sleep(step / speed)

    async def connect_loop():
        while True:
            try:
                await asyncio.wait_for(ch.start(), timeout=5)
                log.info("đã kết nối controller OpenFlow %s:%s", *openflow)
                return
            except (OSError, ValueError) as exc:    # gồm cả TimeoutError
                log.warning("chưa kết nối được controller (%s), thử lại sau %ss",
                            exc or type(exc).__name__, retry_interval)
                await asyncio.sleep(retry_interval)

    @asynccontextmanager
    async def lifespan(app):
        tasks = []
        if autorun:
            tasks.append(asyncio.create_task(sim_loop()))
        if openflow is not None:
            tasks.append(asyncio.create_task(connect_loop()))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if openflow is not None:
                await ch.stop()

    app = FastAPI(lifespan=lifespan)
    app.state.sched = sched
    app.state.channel = ch

    @app.get("/")
    def index():
        return FileResponse(INDEX)

    @app.get("/snapshot")
    def snapshot():
        return metrics.snapshot()

    @app.post("/ping")
    def ping(req: PingRequest):
        src, dst = nodes.get(req.src), nodes.get(req.dst)
        if not isinstance(src, Host) or not isinstance(dst, Host):
            raise HTTPException(404, "src và dst phải là host có trong mạng")
        first = counter["seq"] + 1
        for i in range(req.count):
            counter["seq"] += 1
            # seq của ICMP chỉ có 16 bit; quay vòng để không vượt khi gửi rất nhiều ping
            sched.schedule(i * req.interval, src.ping_ip, dst.ip, counter["seq"] & 0xFFFF)
        return {"seq": first, "count": req.count}

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

    return app