import asyncio
import struct

from .header import (
    OFPT_ECHO_REPLY,
    OFPT_ECHO_REQUEST,
    OFPT_FEATURES_REPLY,
    OFPT_FEATURES_REQUEST,
    OFPT_HELLO,
    Message,
    pack_message,
)
from .reader import MessageReader

# datapath_id(8) n_buffers(4) n_tables(1) auxiliary_id(1) đệm(2) capabilities(4) reserved(4)
FEATURES_REPLY_BODY = struct.Struct("!QIBB2xII")


class OpenFlowSession:
    """Một kết nối OpenFlow từ switch tới controller, giữ mở suốt phiên."""

    def __init__(self, dpid: int) -> None:
        self.dpid = dpid
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._parser = MessageReader()
        # Thông điệp đã tách khỏi luồng byte nhưng chưa trao cho ai.
        # Đây là chỗ giữ lại FEATURES_REQUEST đến cùng đoạn TCP với HELLO.
        self._pending: list[Message] = []

    async def connect(self, host: str, port: int) -> None:
        self._reader, self._writer = await asyncio.open_connection(host, port)

    async def send(self, msg: Message) -> None:
        self._writer.write(pack_message(msg))
        await self._writer.drain()

    async def recv(self) -> Message:
        while not self._pending:
            data = await self._reader.read(4096)
            if data == b"":
                raise ConnectionError("Controller đã đóng kết nối")
            self._pending.extend(self._parser.feed(data))
        return self._pending.pop(0)

    async def handshake(self, xid: int = 1) -> Message:
        await self.send(Message(OFPT_HELLO, xid=xid))
        reply = await self.recv()
        if reply.type != OFPT_HELLO:
            raise ValueError("Thông điệp đầu tiên của controller phải là HELLO")
        return reply

    async def serve(self) -> None:
        """Trả lời FEATURES_REQUEST và ECHO_REQUEST cho tới khi controller đóng kết nối."""
        try:
            while True:
                msg = await self.recv()
                await self._handle(msg)
        except ConnectionError:
            return

    async def _handle(self, msg: Message) -> None:
        if msg.type == OFPT_ECHO_REQUEST:
            await self.send(Message(OFPT_ECHO_REPLY, xid=msg.xid, body=msg.body))
        elif msg.type == OFPT_FEATURES_REQUEST:
            body = FEATURES_REPLY_BODY.pack(self.dpid, 0, 1, 0, 0, 0)
            await self.send(Message(OFPT_FEATURES_REPLY, xid=msg.xid, body=body))
        # Các loại khác (FLOW_MOD, PACKET_OUT...) sẽ xử lý ở bước 4e-4f.

    async def close(self) -> None:
        if self._writer is None:
            return
        writer, self._writer = self._writer, None
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass