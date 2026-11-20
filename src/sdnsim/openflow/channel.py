"""Kênh điều khiển thật: mỗi switch mô phỏng giữ một kết nối OpenFlow TCP tới controller.

Có cùng giao diện với sdnsim.control.channel.Channel mà Switch và Metrics dùng
(register, packet_in, packet_in_count, packet_in_by_switch), nhưng không có controller
trong tiến trình: phía bên kia là bất kỳ controller OpenFlow 1.3 nào.

Hai đồng hồ:
  - PACKET_IN gửi đi ngay khi switch bị table-miss (ghi vào bộ đệm TCP, không chờ).
  - Thông điệp từ controller đến theo giờ thật, nhưng được áp dụng vào switch sau
    `down_latency` giây GIỜ ẢO tính từ lúc nhận, theo đúng thứ tự đến. Nhờ vậy mỗi
    lần đi vòng qua controller luôn tốn ít nhất `down_latency` trong mô phỏng, và
    FLOW_MOD luôn được áp dụng trước PACKET_OUT gửi sau nó.
  - Điều này chỉ có tác dụng khi ai đó chạy mô phỏng (sched.run) trong cùng event loop.

Hỗ trợ MULTIPART loại PORT_DESC (controller 1.3 cần nó để vào chế độ chính).
Thông điệp bị từ chối (codec không hiểu, cổng không tồn tại, loại không hỗ trợ) được
trả lời bằng ERROR và phiên vẫn giữ nguyên. Chỉ khung hỏng (sai độ dài header) mới
làm đóng phiên.
"""

import asyncio
from collections import Counter
from itertools import count

from .codec import (
    OFPBAC_BAD_OUT_PORT, OFPBAC_BAD_TYPE, OFPBRC_BAD_LEN, OFPBRC_BAD_MULTIPART,
    OFPBRC_BAD_TYPE, OFPET_BAD_ACTION, OFPET_BAD_REQUEST, OFPET_FLOW_MOD_FAILED,
    OFPFMFC_UNKNOWN, OFPMP_PORT_DESC, OFPT_BARRIER_REPLY, OFPT_BARRIER_REQUEST,
    OFPT_MULTIPART_REQUEST, OFPT_SET_CONFIG, PortDesc,
    pack_error, pack_packet_in, pack_port_desc_reply, parse_flow_mod,
    parse_multipart_request, parse_packet_out,
)
from .header import OFPT_FLOW_MOD, OFPT_PACKET_OUT, Message
from .session import OpenFlowSession


class OpenFlowChannel:
    def __init__(self, sched, host: str = "127.0.0.1", port: int = 6653,
                 down_latency: float = 0.001):
        self.sched, self.host, self.port, self.down_latency = sched, host, port, down_latency
        self.switches = {}
        self.dpids = {}
        self.packet_in_count = 0
        self.packet_in_by_switch = Counter()
        self.errors = []                     # (tên switch, loại lỗi, mã lỗi) đã gửi cho controller
        self._sessions: dict[str, OpenFlowSession] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._xid = count(1)

    # ------------------------------------------------------ giao diện cho Switch

    def register(self, sw) -> None:
        self.switches[sw.name] = sw
        self.dpids[sw.name] = len(self.dpids) + 1

    def packet_in(self, sw, in_port, pkt) -> None:
        self.packet_in_count += 1
        self.packet_in_by_switch[sw.name] += 1
        session = self._sessions.get(sw.name)
        if session is None:
            return              # chưa kết nối (hoặc đã mất kết nối): gói bị bỏ
        self._send(sw.name, session, pack_packet_in(in_port, pkt, xid=next(self._xid)))

    # --------------------------------------------------------------- vòng đời

    async def start(self) -> None:
        """Kết nối từng switch tới controller và hoàn tất handshake HELLO."""
        try:
            for name in self.switches:
                session = OpenFlowSession(self.dpids[name])
                await session.connect(self.host, self.port)
                try:
                    await session.handshake()
                except BaseException:
                    await session.close()
                    raise
                self._sessions[name] = session
                self._tasks[name] = asyncio.create_task(self._serve(name, session))
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for session in list(self._sessions.values()):
            await session.close()
        self._tasks.clear()
        self._sessions.clear()

    def connected(self, name: str) -> bool:
        return name in self._sessions

    # ------------------------------------------------------------- nội bộ

    async def _serve(self, name: str, session: OpenFlowSession) -> None:
        try:
            await session.serve(lambda msg: self._on_message(name, session, msg))
        except ValueError:
            pass                # khung hỏng: bỏ phiên (quy ước của MessageReader)
        finally:
            self._sessions.pop(name, None)
            await session.close()

    def _send(self, name: str, session: OpenFlowSession, msg: Message) -> None:
        try:
            session.send_nowait(msg)
        except ConnectionError:
            self._sessions.pop(name, None)

    def _later(self, fn, *args) -> None:
        self.sched.schedule(self.down_latency, fn, *args)

    def _error(self, name, session, err_type, code, request) -> None:
        self.errors.append((name, err_type, code))
        self._send(name, session, pack_error(err_type, code, request))

    @staticmethod
    def _port_exists(sw, out_port) -> bool:
        return out_port == "FLOOD" or (isinstance(out_port, int) and out_port in sw.ports)

    def _on_message(self, name: str, session: OpenFlowSession, msg: Message) -> None:
        sw = self.switches[name]
        if msg.type == OFPT_PACKET_OUT:
            self._on_packet_out(sw, session, msg)
        elif msg.type == OFPT_FLOW_MOD:
            self._on_flow_mod(sw, session, msg)
        elif msg.type == OFPT_MULTIPART_REQUEST:
            self._on_multipart(sw, session, msg)
        elif msg.type == OFPT_BARRIER_REQUEST:
            # Trả lời sau các thông điệp đã xếp lịch trước đó, đúng nghĩa của barrier.
            self._later(self._send, name, session, Message(OFPT_BARRIER_REPLY, xid=msg.xid))
        elif msg.type == OFPT_SET_CONFIG:
            pass                # miss_send_len và cờ: switch mô phỏng luôn gửi nguyên gói
        else:
            self._error(sw.name, session, OFPET_BAD_REQUEST, OFPBRC_BAD_TYPE, msg)

    def _on_multipart(self, sw, session, msg: Message) -> None:
        """Chỉ trả lời mô tả cổng (PORT_DESC). Controller OpenFlow 1.3 như os-ken/Ryu gửi
        yêu cầu này ngay sau handshake và chỉ bắt đầu xử lý PACKET_IN khi nhận được trả lời."""
        try:
            mp_type, _flags, _body = parse_multipart_request(msg)
        except ValueError:
            self._error(sw.name, session, OFPET_BAD_REQUEST, OFPBRC_BAD_LEN, msg)
            return
        if mp_type != OFPMP_PORT_DESC:
            self._error(sw.name, session, OFPET_BAD_REQUEST, OFPBRC_BAD_MULTIPART, msg)
            return
        dpid = self.dpids[sw.name]
        ports = [PortDesc(p, f"02:00:00:00:{dpid & 0xFF:02x}:{p & 0xFF:02x}",
                          f"{sw.name}-eth{p}"[:15])
                 for p in sorted(sw.ports)]
        # Gửi ngay, không qua hàng đợi giờ ảo: đây là thông tin cấu hình, không phải lưu lượng.
        self._send(sw.name, session, pack_port_desc_reply(ports, xid=msg.xid))

    def _on_packet_out(self, sw, session, msg: Message) -> None:
        try:
            out_port, in_port, pkt = parse_packet_out(msg)
        except ValueError:
            self._error(sw.name, session, OFPET_BAD_ACTION, OFPBAC_BAD_TYPE, msg)
            return
        if not self._port_exists(sw, out_port):
            self._error(sw.name, session, OFPET_BAD_ACTION, OFPBAC_BAD_OUT_PORT, msg)
            return
        self._later(sw.packet_out, pkt, out_port, in_port)

    def _on_flow_mod(self, sw, session, msg: Message) -> None:
        try:
            spec = parse_flow_mod(msg)
        except ValueError:
            self._error(sw.name, session, OFPET_FLOW_MOD_FAILED, OFPFMFC_UNKNOWN, msg)
            return
        if spec.match == {} and spec.out_port == "CONTROLLER":
            return              # flow table-miss: switch mô phỏng đã tự gửi PACKET_IN khi miss
        if spec.out_port == "CONTROLLER":
            self._error(sw.name, session, OFPET_FLOW_MOD_FAILED, OFPFMFC_UNKNOWN, msg)
            return
        if not self._port_exists(sw, spec.out_port):
            self._error(sw.name, session, OFPET_BAD_ACTION, OFPBAC_BAD_OUT_PORT, msg)
            return
        self._later(sw.flow_mod, spec.match, spec.out_port, spec.priority, spec.idle_timeout)