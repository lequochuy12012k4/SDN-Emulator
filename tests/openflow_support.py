"""Công cụ dùng chung cho các test OpenFlow: controller giả và bộ chạy giờ ảo."""

import asyncio
import struct

from sdnsim.openflow.codec import (
    OFPT_ERROR, pack_flow_mod, pack_packet_out, parse_packet_in,
)
from sdnsim.openflow.header import (
    Message, OFPT_FEATURES_REPLY, OFPT_FEATURES_REQUEST, OFPT_HELLO, OFPT_PACKET_IN,
    pack_message,
)
from sdnsim.openflow.reader import MessageReader


class FakeController:
    """Controller OpenFlow 1.3 tối giản: handshake, cài flow table-miss, rồi học MAC."""

    def __init__(self, on_connect=None, idle_timeout=None):
        self.idle_timeout = idle_timeout    # giây nguyên đặt vào mọi FLOW_MOD học được; None = không hết hạn
        self.on_connect = on_connect        # hàm(send) chạy sau khi biết dpid của switch
        self.tables = {}                    # dpid -> {mac: cổng}
        self.ready = set()                  # dpid đã hoàn tất handshake
        self.senders = {}                   # dpid -> hàm gửi, để test chủ động gửi yêu cầu
        self.received = []                  # (dpid, Message) mọi thông điệp sau handshake
        self.port = None
        self._server = None
        self._writers = []

    async def start(self, port=0):
        self._server = await asyncio.start_server(self._on_connection, "127.0.0.1", port)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self):
        self._server.close()
        self.disconnect_all()
        await asyncio.wait_for(self._server.wait_closed(), 3)

    def disconnect_all(self):
        for writer in self._writers:
            writer.close()

    async def _on_connection(self, reader, writer):
        self._writers.append(writer)
        parser, pending = MessageReader(), []

        async def recv():
            while not pending:
                data = await reader.read(4096)
                if not data:
                    raise ConnectionError("switch đã đóng kết nối")
                pending.extend(parser.feed(data))
            return pending.pop(0)

        def send(msg):
            writer.write(pack_message(msg))

        try:
            send(Message(OFPT_HELLO, xid=1))
            await recv()                                             # HELLO của switch
            send(Message(OFPT_FEATURES_REQUEST, xid=2))
            features = await recv()
            assert features.type == OFPT_FEATURES_REPLY
            dpid = struct.unpack("!Q", features.body[:8])[0]
            send(pack_flow_mod({}, "CONTROLLER", priority=0))        # table-miss như simple_switch_13
            self.senders[dpid] = send
            self.ready.add(dpid)
            if self.on_connect:
                self.on_connect(send)
            while True:
                msg = await recv()
                self.received.append((dpid, msg))
                if msg.type == OFPT_PACKET_IN:
                    self._learn(dpid, msg, send)
        except ConnectionError:
            pass
        finally:
            writer.close()

    def _learn(self, dpid, msg, send):
        in_port, pkt = parse_packet_in(msg)
        table = self.tables.setdefault(dpid, {})
        table[pkt.eth_src] = in_port
        out = table.get(pkt.eth_dst, "FLOOD")
        if out != "FLOOD":
            send(pack_flow_mod(
                {"in_port": in_port, "eth_dst": pkt.eth_dst, "eth_src": pkt.eth_src}, out,
                idle_timeout=self.idle_timeout))
        send(pack_packet_out(out, pkt, in_port=in_port))

    def errors(self):
        return [(dpid, msg) for dpid, msg in self.received if msg.type == OFPT_ERROR]


async def advance(sched, condition, step=0.005, timeout=5.0):
    """Chạy giờ ảo từng bước nhỏ, xen kẽ nhường event loop cho TCP, tới khi điều kiện đúng."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise TimeoutError("điều kiện không đạt trong thời hạn")
        sched.run(until=sched.now + step)
        await asyncio.sleep(0.002)


if __name__ == "__main__":
    # Chạy controller giả để thử tay với dashboard:
    #   python tests\openflow_support.py [cổng] [idle_timeout]
    # idle_timeout là số giây nguyên đặt vào mọi flow học được; bỏ trống = flow không hết hạn.
    import sys

    listen_port = int(sys.argv[1]) if len(sys.argv) > 1 else 6653
    idle = int(sys.argv[2]) if len(sys.argv) > 2 else None

    async def main():
        controller = FakeController(idle_timeout=idle)
        await controller.start(listen_port)
        note = f", idle_timeout={idle}s" if idle else ", không đặt idle_timeout"
        print(f"controller giả đang nghe tại 127.0.0.1:{listen_port}{note} (Ctrl+C để dừng)", flush=True)
        await asyncio.Event().wait()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass