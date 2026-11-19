import asyncio
import struct

from sdnsim.openflow.header import (
    Message, pack_message, OFPT_HELLO, OFPT_ECHO_REQUEST, OFPT_ECHO_REPLY,
    OFPT_FEATURES_REQUEST, OFPT_FEATURES_REPLY,
)
from sdnsim.openflow.reader import MessageReader
from sdnsim.openflow.session import OpenFlowSession


async def read_one(reader, mr, pending):
    while not pending:
        data = await reader.read(4096)
        if not data:
            raise ConnectionError("đóng sớm")
        pending.extend(mr.feed(data))
    return pending.pop(0)


def test_message_right_after_hello_is_not_lost():
    async def run():
        async def handler(reader, writer):
            # HELLO và FEATURES_REQUEST trong CÙNG một lần ghi
            writer.write(pack_message(Message(OFPT_HELLO, xid=1))
                         + pack_message(Message(OFPT_FEATURES_REQUEST, xid=2)))
            await writer.drain()
            await reader.read(4096)
            writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        s = OpenFlowSession(dpid=1)
        try:
            await asyncio.wait_for(s.connect("127.0.0.1", port), 3)
            hello = await asyncio.wait_for(s.handshake(), 3)
            nxt = await asyncio.wait_for(s.recv(), 3)
            await s.close()
        finally:
            server.close()
            await server.wait_closed()
        return hello, nxt

    hello, nxt = asyncio.run(run())
    assert hello.type == OFPT_HELLO
    assert (nxt.type, nxt.xid) == (OFPT_FEATURES_REQUEST, 2)


def test_session_answers_features_and_echo():
    got = {}

    async def run():
        async def handler(reader, writer):
            mr, pending = MessageReader(), []
            writer.write(pack_message(Message(OFPT_HELLO, xid=1)))
            await writer.drain()
            await read_one(reader, mr, pending)               # HELLO của switch
            writer.write(pack_message(Message(OFPT_FEATURES_REQUEST, xid=3)))
            await writer.drain()
            got["features"] = await read_one(reader, mr, pending)
            writer.write(pack_message(Message(OFPT_ECHO_REQUEST, xid=7, body=b"ping")))
            await writer.drain()
            got["echo"] = await read_one(reader, mr, pending)
            writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        s = OpenFlowSession(dpid=0xABC)
        try:
            await asyncio.wait_for(s.connect("127.0.0.1", port), 3)
            await asyncio.wait_for(s.handshake(), 3)
            await asyncio.wait_for(s.serve(), 3)   # trả về khi controller đóng
        finally:
            await s.close()
            server.close()
            await server.wait_closed()

    asyncio.run(run())

    f = got["features"]
    assert (f.type, f.xid, len(f.body)) == (OFPT_FEATURES_REPLY, 3, 24)
    assert struct.unpack("!Q", f.body[:8])[0] == 0xABC
    e = got["echo"]
    assert (e.type, e.xid, e.body) == (OFPT_ECHO_REPLY, 7, b"ping")